import asyncio
import base64
import importlib.util
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import chassis_pb2
import grpc
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from PIL import Image
from robonix_api import ATLAS
import std_srvs_pb2


def load_chassis_services():
    """This package's generated stubs have no chassis calls; the chassis package's do."""
    path = (Path(__file__).resolve().parents[2] / "go2w_chassis_motion/rbnx-build/codegen"
            / "proto_gen/robonix_contracts_pb2_grpc.py")
    spec = importlib.util.spec_from_file_location("chassis_contracts_pb2_grpc", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


services = load_chassis_services()


def publish_status(path, status):
    """Latest state for the display-only GUI. A failed write must never affect the robot."""
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as out:
            json.dump({**status, "time": time.time()}, out, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except (OSError, TypeError, ValueError):
        pass


class Robot:
    def __init__(self, settings, consumer_id):
        self.settings = settings
        self.consumer_id = consumer_id
        self.lock = threading.Lock()
        self.active_call = None
        self.last_execution = {"stop_requested": False, "stop_confirmed": False}

    @contextmanager
    def connect(self, provider_id, contract):
        with ATLAS.connect_capability(consumer_id=self.consumer_id,
                                      provider_id=provider_id,
                                      contract_id=contract,
                                      transport="grpc") as connection:
            with grpc.insecure_channel(connection.endpoint.removeprefix("http://")) as channel:
                yield channel

    def capture_rgb(self):
        async def fetch(endpoint):
            async with streamablehttp_client(endpoint) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("snapshot", {})
                    if result.isError:
                        message = "; ".join(
                            item.text for item in result.content if item.type == "text"
                        )
                        raise RuntimeError(f"CAMERA_UNAVAILABLE: {message}")
                    if isinstance(result.structuredContent, dict):
                        return result.structuredContent
                    content = next(
                        item.text for item in result.content if item.type == "text"
                    )
                    return json.loads(content)

        with ATLAS.connect_capability(
            consumer_id=self.consumer_id,
            provider_id=self.settings.camera_provider_id,
            contract_id="robonix/primitive/camera/snapshot",
            transport="mcp",
        ) as connection:
            payload = asyncio.run(asyncio.wait_for(
                fetch(connection.endpoint), timeout=self.settings.capture_timeout_s,
            ))
        if not isinstance(payload, dict):
            raise ValueError("CAMERA_INVALID: snapshot payload must be an object")
        header = payload["header"]
        stamp = header["stamp"]
        frame_id = header["frame_id"]
        width = int(payload["width"])
        height = int(payload["height"])
        if (payload["encoding"] != "jpeg" or width <= 0 or height <= 0
                or width > 4096 or height > 4096):
            raise ValueError("CAMERA_INVALID: JPEG encoding or dimensions are invalid")
        if frame_id != self.settings.camera_frame_id:
            raise RuntimeError("CAMERA_FRAME_INVALID: image frame is unexpected")
        encoded = payload["data"]
        if not isinstance(encoded, str):
            raise ValueError("CAMERA_INVALID: MCP image data must be base64 text")
        image_jpeg = base64.b64decode(encoded, validate=True)
        if not image_jpeg or len(image_jpeg) > 4 * 1024 * 1024:
            raise ValueError("CAMERA_INVALID: JPEG payload size is invalid")
        if int(payload["step"]) != len(image_jpeg):
            raise ValueError("CAMERA_INVALID: JPEG payload length differs from metadata")
        with Image.open(BytesIO(image_jpeg)) as image:
            if image.format != "JPEG" or image.size != (width, height):
                raise ValueError("CAMERA_INVALID: JPEG content does not match its metadata")
            image.verify()
        seconds = int(stamp["sec"])
        nanoseconds = int(stamp["nanosec"])
        if nanoseconds < 0 or nanoseconds >= 1_000_000_000:
            raise ValueError("CAMERA_INVALID: timestamp nanoseconds are invalid")
        stamp_ns = seconds * 1_000_000_000 + nanoseconds
        age_s = (time.time_ns() - stamp_ns) / 1_000_000_000
        if stamp_ns <= 0 or age_s < -0.05 or age_s > self.settings.max_sensor_age_s:
            raise RuntimeError("CAMERA_STALE: image timestamp is outside its freshness limit")
        return image_jpeg, stamp_ns, age_s, frame_id

    def evaluate(self, request_id):
        try:
            image_jpeg, image_stamp_ns, camera_age_s, camera_frame_id = self.capture_rgb()
        except RuntimeError:
            raise
        except Exception as error:
            # The snapshot call fails as an exception group when the camera briefly has no
            # fresh frame. Report it as stale so the task skips this round.
            raise RuntimeError(f"CAMERA_STALE: snapshot failed: {error}") from error
        # The robot stands still between steps, so the distance is read while the model runs.
        with ThreadPoolExecutor(max_workers=1) as pool:
            lidar_check = pool.submit(self.check_lidar)
            with httpx.Client(trust_env=False, timeout=self.settings.model_timeout_s) as client:
                response = client.post(
                    self.settings.model_url.rstrip("/") + "/answer",
                    content=image_jpeg,
                    headers={"Content-Type": "image/jpeg", "X-Request-ID": request_id},
                )
                response.raise_for_status()
            lidar = lidar_check.result()
        payload = response.json()
        answers = payload["answers"]
        person = float(answers["person"])
        prohibition = float(answers["prohibition"])
        obstacle = float(answers["obstacle"])
        if (not math.isfinite(person) or not math.isfinite(prohibition)
                or not math.isfinite(obstacle)
                or not 0 <= person <= 1 or not 0 <= prohibition <= 1
                or not 0 <= obstacle <= 1):
            raise ValueError("INVALID_MODEL_RESPONSE: answer probabilities are invalid")
        semantics_clear = (person < self.settings.clear_probability_limit
                           and prohibition < self.settings.clear_probability_limit
                           and obstacle < self.settings.clear_probability_limit)
        allowed = semantics_clear and lidar["clear"]
        if person >= self.settings.clear_probability_limit:
            reason = "person_or_uncertain"
        elif prohibition >= self.settings.clear_probability_limit:
            reason = "prohibition_or_uncertain"
        elif obstacle >= self.settings.clear_probability_limit:
            reason = "obstacle_or_uncertain"
        elif not lidar["clear"]:
            reason = lidar["reason"]
        else:
            reason = "clear"
        decision = {
            "decision_id": request_id,
            "forward_allowed": allowed,
            "person_probability": person,
            "prohibition_probability": prohibition,
            "obstacle_probability": obstacle,
            "lidar": lidar,
            "camera_age_s": camera_age_s,
            "camera_frame_id": camera_frame_id,
            "camera_source_stamp_ns": image_stamp_ns,
            "model_usage": payload.get("usage", {}),
            "reason": reason,
        }
        publish_status("/tmp/go2w_decision.json", decision)
        return decision

    def check_lidar(self):
        """Distance comes from the Observer corridor. Anything unreadable or stale blocks motion.

        A single frame can miss an object (the scan is sparse), so several frames are read
        and the nearest return among them decides.
        """
        stamps, distances, points = set(), [], 0
        deadline = time.monotonic() + self.settings.lidar_timeout_s
        try:
            with httpx.Client(trust_env=False, timeout=self.settings.lidar_timeout_s) as client:
                while len(stamps) < self.settings.lidar_frames and time.monotonic() < deadline:
                    response = client.get(self.settings.lidar_url)
                    response.raise_for_status()
                    cloud = response.json()
                    stamp = float(cloud["source_stamp"])
                    if stamp in stamps:
                        time.sleep(0.05)
                        continue
                    stamps.add(stamp)
                    points += int(cloud["corridor_point_count"])
                    if cloud["nearest_corridor_distance_m"] is not None:
                        distances.append(float(cloud["nearest_corridor_distance_m"]))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            return {"clear": False, "reason": "lidar_unavailable", "error": str(error)}
        if len(stamps) < self.settings.lidar_frames:
            return {"clear": False, "reason": "lidar_stale", "frames": len(stamps)}
        age_s = time.time() - max(stamps)
        distance = min(distances) if distances else None
        summary = {"distance_m": distance, "corridor_points": points, "age_s": age_s,
                   "frames": len(stamps)}
        if age_s < -0.05 or age_s > self.settings.max_sensor_age_s:
            return {**summary, "clear": False, "reason": "lidar_stale"}
        if distance is None or not math.isfinite(distance):
            return {**summary, "clear": False, "reason": "lidar_no_return"}
        if distance <= self.settings.stop_distance_m:
            return {**summary, "clear": False, "reason": "lidar_blocked"}
        return {**summary, "clear": True, "reason": "clear"}

    def execute_forward(self, cancelled):
        self.last_execution = {
            "status": "dispatched",
            "speed_mps": self.settings.forward_speed_mps,
            "duration_s": self.settings.forward_duration_s,
            "stop_requested": False,
            "stop_confirmed": False,
        }
        with self.connect(self.settings.chassis_provider_id,
                          "robonix/primitive/chassis/forward") as channel:
            stub = services.RobonixPrimitiveChassisForwardStub(channel)
            with self.lock:
                if cancelled.is_set():
                    raise RuntimeError("TASK_CANCELLED")
                call = stub.ExecuteMoveCommand.future(
                    chassis_pb2.ExecuteMoveCommand_Request(
                        command=chassis_pb2.MoveCommand(
                            linear_x=self.settings.forward_speed_mps,
                            duration_sec=self.settings.forward_duration_s,
                        ),
                    ),
                    timeout=self.settings.forward_duration_s + 5.0,
                )
                self.active_call = call
            try:
                response = call.result()
                result = json.loads(response.status.data)
                # "not_observed": the burst reached the firmware and stopped, but this short,
                # slow step did not reach the feedback threshold. "not_dispatched" still fails.
                if (result.get("status") not in ("completed", "not_observed")
                        or result.get("stop_acknowledged") is not True):
                    raise RuntimeError("MOTION_NOT_CONFIRMED: forward result lacks completion or stop "
                                       "evidence: " + response.status.data[:300])
                self.last_execution.update(result)
                return result
            finally:
                with self.lock:
                    self.active_call = None
                self.last_execution["stop_requested"] = True
                stop_result = self.stop()
                self.last_execution["stop_confirmed"] = True
                self.last_execution["stop_result"] = stop_result

    def validate_motion_session(self):
        with self.connect(self.settings.chassis_provider_id,
                          "robonix/primitive/chassis/motion_status") as channel:
            response = services.RobonixPrimitiveChassisMotionStatusStub(channel).Trigger(
                std_srvs_pb2.Trigger_Request(), timeout=2.0,
            )
        if not response.success:
            raise RuntimeError("MOTION_STATUS_UNAVAILABLE: chassis motion status failed")
        state = json.loads(response.message)
        guard = state.get("guard")
        if (state.get("session_enabled") is not True
                or state.get("faulted") is not False
                or not isinstance(guard, dict)
                or guard.get("ready") is not True
                or guard.get("session_enabled") is not True
                or guard.get("faulted") is not False):
            raise RuntimeError("MOTION_NOT_ENABLED: an operator-enabled healthy session is required")
        return state

    def stop(self):
        with self.connect(self.settings.chassis_provider_id,
                          "robonix/primitive/chassis/stop") as channel:
            # The chassis_pb2 on this path predates Stop. Its request is empty and its
            # response has the same fields as Trigger, so the call is made directly.
            call = channel.unary_unary(
                "/robonix.contracts.RobonixPrimitiveChassisStop/Stop",
                request_serializer=lambda _: b"",
                response_deserializer=std_srvs_pb2.Trigger_Response.FromString,
            )
            response = call(None, timeout=2.0)
        result = json.loads(response.message)
        if (not response.success or result.get("status") != "stopped"
                or result.get("stop_acknowledged") is not True):
            raise RuntimeError("STOP_UNCONFIRMED: chassis service did not confirm StopMove")
        return result

    def cancel_active(self):
        with self.lock:
            call = self.active_call
        if call is not None:
            call.cancel()
