"""Robonix lifecycle and standard capabilities for the native Foxy observer."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import urllib.request

import yaml
from robonix_api import Err, Ok, Primitive
import health_pb2
from builtin_interfaces_mcp import Time
from sensor_msgs_mcp import Image
from std_msgs_mcp import Empty, Header

ROOT = Path(__file__).resolve().parents[3]
BUILD = ROOT / "rbnx-build/native"
provider = Primitive(id="go2w_native", namespace="robonix/primitive")
config = yaml.safe_load((ROOT / "native/config.yaml").read_text())
children = []
stop_event = threading.Event()
declared = False
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(path):
    with opener.open(f"http://127.0.0.1:{config['http_port']}{path}", timeout=2) as response:
        return json.load(response)


def stop_children():
    stop_event.set()
    for process in reversed(children):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for process in reversed(children):
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
    children.clear()


@provider.on_init
def initialize(settings):
    if settings or config.get("allow_motion") is not False:
        return Err("The native sensor profile does not accept motion/config overrides")
    stop_event.clear()
    # Port ownership is checked before spawning: never adopt an existing server.
    import socket
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", config["http_port"]))
    except OSError as error:
        return Err(f"Native telemetry port is already occupied: {error}")
    try:
        worker = provider.spawn(
            ["bash", str(ROOT / "native/ros-python.sh"), str(ROOT / "native/ros_worker.py"),
             "--config", str(ROOT / "native/config.yaml")],
            log="native-ros-worker.log", cwd=ROOT,
        )
        children.append(worker)
        if config["camera_enabled"]:
            socket_path = Path(f"/run/user/{os.getuid()}/go2-native/camera.sock")
            socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            daemon = BUILD / "camera/install/bin/go2_camera_daemon"
            children.append(provider.spawn(
                [str(daemon), "--interface", config["network_interface"],
                 "--socket", str(socket_path), "--fps", "5",
                 "--domain-id", str(config["ros_domain_id"])],
                env={"LD_LIBRARY_PATH": str(BUILD / "camera/install/lib")},
                log="native-camera-reader.log", cwd=ROOT,
            ))
            bridge = BUILD / "ros/install/lib/go2_sensors/go2_camera_bridge"
            children.append(provider.spawn(
                ["bash", str(ROOT / "native/ros-exec.sh"), str(bridge),
                 "--ros-args", "-p", f"camera.socket_path:={socket_path}",
                 "-p", f"camera.image_topic:={config['sources']['camera']}",
                 "-p", "camera.camera_info_topic:=/robonix/go2/camera/camera_info",
                 "-p", "status.topic:=/robonix/go2/camera/status",
                 "-p", f"camera.frame_id:={config['camera_frame']}"],
                log="native-camera-bridge.log", cwd=ROOT,
            ))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in children):
                raise RuntimeError("a native sensor child exited during initialization")
            try:
                data = fetch("/status")
                if all(data["sources"].get(key, {}).get("healthy")
                       for key in ("lidar", "imu", "lowstate")):
                    return Ok()
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        raise RuntimeError("No fresh lidar, IMU or battery samples within 20 seconds")
    except Exception as error:
        stop_children()
        return Err(str(error))


@provider.on_activate
def activate():
    global declared
    if not children or any(child.poll() is not None for child in children):
        return Err("The native observer is not running")
    if not declared:
        for contract, key in [
            ("robonix/primitive/lidar/lidar3d", "lidar"),
            ("robonix/primitive/imu/imu", "imu"),
        ]:
            provider.declare_ros2_topic(contract, config["sources"][key], qos="best_effort")
        if config["camera_enabled"]:
            provider.declare_ros2_topic(
                "robonix/primitive/camera/rgb", config["sources"]["camera"], qos="best_effort",
            )
        declared = True
    return Ok()


@provider.mcp("robonix/primitive/camera/snapshot")
def camera_snapshot(request: Empty) -> Image:
    """Read one fresh front-camera JPEG. This operation sends no motion command."""
    payload = fetch("/camera")
    stamp = payload["source_stamp"]
    seconds = int(stamp)
    data = base64.b64decode(payload["data"], validate=True)
    return Image(
        header=Header(stamp=Time(sec=seconds, nanosec=int((stamp - seconds) * 1e9)),
                      frame_id=payload["frame_id"]),
        width=payload["width"], height=payload["height"], encoding="jpeg",
        is_bigendian=0, step=len(data), data=data,
    )


def health():
    try:
        data = fetch("/status")["health"]
    except (OSError, ValueError):
        # Unknown hardware values stay unknown when the ROS worker is unavailable.
        data = {"voltage": -1, "charging": False, "remaining_s": -1, "readings": []}
    return health_pb2.HealthState(**data)


@provider.grpc("robonix/primitive/health/state")
def get_health(request):
    return health_pb2.GetHealthState_Response(state=health())


@provider.grpc("robonix/primitive/health/stream")
def stream_health(request, context):
    while context.is_active() and not stop_event.is_set():
        yield health()
        stop_event.wait(0.5)


@provider.on_deactivate
def deactivate():
    stop_children()
    return Ok()


@provider.on_shutdown
def shutdown():
    stop_children()
    return Ok()


if __name__ == "__main__":
    provider.run()
