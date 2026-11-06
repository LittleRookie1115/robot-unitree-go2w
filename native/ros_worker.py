#!/usr/bin/env python3
"""Foxy sensor observer. Creates subscriptions and loopback HTTP; no control client."""
import argparse
import base64
import json
from pathlib import Path
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import rclpy
from rclpy.qos import qos_profile_sensor_data
from nav_msgs.msg import Odometry
from sensor_msgs.msg import CompressedImage, Image, Imu, PointCloud2
from sensor_msgs_py import point_cloud2
from unitree_go.msg import LowState, SportModeState
import yaml

from telemetry import build_health, freshness
from lidar_guard import summarize_lidar_points


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text())
    if config.get("allow_motion") is not False:
        raise SystemExit("The native observer requires allow_motion: false")
    records, payloads, subscriptions = {}, {}, []
    lock = threading.RLock()
    stop = threading.Event()
    rclpy.init()
    node = rclpy.create_node(
        "go2w_native_sensor_observer", enable_rosout=False, start_parameter_services=False,
    )
    started = time.monotonic()
    processed = {}
    observed_topics = dict(config["sources"])
    compressed_camera = bool(config.get("camera_observer_topic"))
    if compressed_camera:
        observed_topics["camera"] = config["camera_observer_topic"]

    def receive(key, message):
        now = time.monotonic()
        # Camera input is already capped at 5 Hz by our reader. A second
        # 200 ms gate drops alternating frames when their arrival jitters.
        interval = 0.0 if key == "camera" else (0.2 if key == "lidar" else 0.05)
        if now - processed.get(key, 0) < interval:
            return
        processed[key] = now
        record = dict(received_monotonic=now, received_at_unix=time.time())
        if hasattr(message, "header"):
            h = message.header
            record.update(source_stamp=h.stamp.sec + h.stamp.nanosec * 1e-9,
                          frame_id=h.frame_id)
        if key == "lowstate":
            record.update(voltage=float(message.power_v),
                          battery_percent=float(message.bms_state.soc),
                          motor_temperatures_c=[int(m.temperature) for m in message.motor_state])
        elif key == "sport":
            record.update(error_code=int(message.error_code), mode=int(message.mode),
                          source_stamp=message.stamp.sec + message.stamp.nanosec * 1e-9,
                          velocity_mps=[float(v) for v in message.velocity])
        elif key == "odom":
            p = message.pose.pose.position
            q = message.pose.pose.orientation
            record.update(child_frame_id=message.child_frame_id,
                          position=[p.x, p.y, p.z],
                          quaternion=[q.x, q.y, q.z, q.w])
        elif key == "imu":
            record["angular_velocity"] = [message.angular_velocity.x,
                                          message.angular_velocity.y,
                                          message.angular_velocity.z]
        healthy, _ = freshness(record, time.time(), now, config["max_source_age_s"],
                               config["max_source_future_s"], config["max_receipt_age_s"])
        if key == "camera":
            if compressed_camera:
                if len(message.data) > 4 * 1024 * 1024:
                    return
                pixels = cv2.imdecode(np.frombuffer(message.data, dtype=np.uint8),
                                      cv2.IMREAD_COLOR)
                if pixels is None or max(pixels.shape[:2]) > 4096:
                    return
                height, width = pixels.shape[:2]
                record.update(width=width, height=height, encoding="jpeg")
                if healthy:
                    with lock:
                        payloads["camera"] = {
                            "data": base64.b64encode(message.data).decode(),
                            "encoding": "jpeg", "width": width, "height": height,
                            "frame_id": message.header.frame_id,
                            "source_stamp": record["source_stamp"],
                        }
            else:
                record.update(width=message.width, height=message.height,
                              encoding=message.encoding)
            if (not compressed_camera and healthy
                    and message.encoding in ("rgb8", "bgr8") and 0 < message.width <= 4096):
                pixels = np.frombuffer(message.data, dtype=np.uint8).reshape(
                    message.height, message.step)[:, :message.width * 3].reshape(
                        message.height, message.width, 3)
                if message.encoding == "rgb8":
                    pixels = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
                ok, jpeg = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    with lock:
                        payloads["camera"] = {
                            "data": base64.b64encode(jpeg).decode(),
                            "encoding": "jpeg", "width": message.width, "height": message.height,
                            "frame_id": message.header.frame_id,
                            "source_stamp": record["source_stamp"],
                        }
        elif key == "lidar":
            record.update(points=message.width * message.height,
                          fields=[field.name for field in message.fields])
            if healthy:
                guard = config["lidar_guard"]
                stride = max(1, message.width * message.height // 800)
                summary = summarize_lidar_points(
                    point_cloud2.read_points(message, field_names=("x", "y", "z"), skip_nans=True),
                    guard, stride,
                )
                with lock:
                    payloads["lidar"] = {
                        **summary,
                        "frame_id": record["frame_id"],
                        "source_stamp": record["source_stamp"],
                        "received_at_unix": record["received_at_unix"],
                        "guard": dict(guard),
                    }
        with lock:
            prior = records.get(key, {})
            record["observed_samples"] = prior.get("observed_samples", 0) + 1
            records[key] = record

    types = dict(lidar=PointCloud2, imu=Imu, odom=Odometry, sport=SportModeState,
                 lowstate=LowState, camera=CompressedImage if compressed_camera else Image)
    for key, topic in observed_topics.items():
        subscriptions.append(node.create_subscription(
            types[key], topic, lambda msg, selected=key: receive(selected, msg),
            qos_profile_sensor_data,
        ))

    def status():
        with lock:
            samples = {key: dict(value) for key, value in records.items()}
        for key, value in samples.items():
            valid, reason = freshness(
                value, time.time(), time.monotonic(), config["max_source_age_s"],
                config["max_source_future_s"], config["max_receipt_age_s"],
                require_stamp=key not in ("sport", "lowstate"),
            )
            value.update(healthy=valid, freshness=reason,
                         receipt_age_s=time.monotonic() - value["received_monotonic"])
            if value.get("source_stamp", 0) > 0:
                value["source_age_s"] = time.time() - value["source_stamp"]
            value["topic"] = observed_topics[key]
            value.pop("received_monotonic")
        return dict(motion_enabled=False, motion_enabled_scope="sensor_observer_only",
                    motion_state_authority="robonix/primitive/chassis/motion_status",
                    robot_variant="go2w", sources=samples,
                    uptime_s=time.monotonic() - started,
                    health=build_health(records, config))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state = status()
            code, data, kind = 200, None, "application/json"
            if self.path in ("/status", "/api/state"):
                data = json.dumps(state, allow_nan=False).encode()
            elif self.path in ("/camera", "/cloud"):
                key = "camera" if self.path == "/camera" else "lidar"
                with lock:
                    payload = payloads.get(key)
                if not state["sources"].get(key, {}).get("healthy") or not payload:
                    code, payload = 503, {"error": key + " has no fresh sample"}
                data = json.dumps(payload, allow_nan=False).encode()
            elif self.path == "/":
                kind = "text/html; charset=utf-8"
                data = (Path(__file__).with_name("index.html")).read_bytes()
            else:
                code, data = 404, b'{"error":"not found"}'
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", int(config["http_port"])), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print("Native Foxy observer on 127.0.0.1:%s; motion unavailable" % config["http_port"],
          flush=True)
    try:
        while not stop.is_set():
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        server.shutdown()
        server.server_close()
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
