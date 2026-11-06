"""Subprocess fixture: replace ROS imports before running the production worker."""
import json
import os
from pathlib import Path
import runpy
import sys
import time
import types


def trace(event, **values):
    print(json.dumps({"event": event, **values}), file=sys.stderr, flush=True)


def module(name, **values):
    result = types.ModuleType(name)
    result.__dict__.update(values)
    sys.modules[name] = result
    return result


class Twist:
    def __init__(self):
        self.linear = types.SimpleNamespace(x=0.0, y=0.0, z=0.0)
        self.angular = types.SimpleNamespace(x=0.0, y=0.0, z=0.0)


class SetBool:
    class Request:
        def __init__(self, data):
            self.data = data


class Trigger:
    class Request:
        pass


class Publisher:
    def publish(self, message):
        trace("publish", vx=message.linear.x, vy=message.linear.y, wz=message.angular.z)
        if message.linear.x or message.linear.y or message.angular.z:
            node.moves += 1


class Client:
    def __init__(self, service):
        self.service = service

    def service_is_ready(self):
        return True

    def call_async(self, request):
        success, message = True, ""
        if hasattr(request, "data"):
            node.armed = bool(request.data)
            trace("arm" if request.data else "disarm", data=request.data)
        else:
            assert self.service in ("/go2_chassis/stand_up", "/go2_chassis/stand_down",
                                    "/go2_chassis/balance_stand", "/go2_chassis/finish_posture",
                                    "/go2_chassis/finish")
            if self.service.endswith("/finish"):
                node.stops += 1
            event = ("finish" if self.service.endswith("/finish") else
                     "finish_posture" if self.service.endswith("/finish_posture") else "posture")
            if event == "posture":
                node.posture_attempts += 1
                node.posture_code = int(os.environ.get("GO2_TEST_POSTURE_CODE", "0"))
                success = node.posture_code == 0
                if not success:
                    message = "native posture SDK command failed with code " + str(node.posture_code)
            trace(event, service=self.service)
        return types.SimpleNamespace(
            done=lambda: True, result=lambda: types.SimpleNamespace(success=success, message=message))


class Node:
    def __init__(self):
        self.callbacks = {}
        self.timers = []
        self.armed = os.environ.get("GO2_TEST_START_DISARMED") != "1"
        self.moves = 0
        self.stops = 0
        self.posture_attempts = 0
        self.posture_code = 0

    def create_publisher(self, *_):
        return Publisher()

    def create_client(self, cls, service):
        return Client(service)

    def create_subscription(self, cls, topic, callback, qos):
        self.callbacks[topic] = callback
        return object()

    def create_timer(self, period, callback):
        self.timers.append(callback)
        return object()

    def count_publishers(self, topic):
        return 1

    def count_subscribers(self, topic):
        return 1

    def destroy_node(self):
        pass

    def spin(self):
        faulted = os.environ.get("GO2_TEST_GUARD_FAULT") == "1"
        values = {
            "guard_state": "FAULT" if faulted else ("ARMED" if self.armed else "DISARMED"),
            "daemon_armed": "true" if self.armed else "false", "allow_motion": "true",
            "state_valid": "true", "source_stamp_status": "fresh",
            "motion_profile": "native-go2w-manual-v2",
            "state_time_basis": "dds-source-lowstate-tick-v1",
            "sport_dds_source_ns": str(time.time_ns()),
            "movement_mode_ready": "true" if os.environ.get("GO2_TEST_MODE", "1") in ("1","3") else "false",
            "mode": os.environ.get("GO2_TEST_MODE", "1"),
            "motion_evidence_instance": "fixture-worker",
            "move_dispatch_count": str(self.moves),
            "move_attempt_count": str(self.moves),
            "stop_ack_count": str(self.stops),
            "last_move_remote_response_verified": "true",
            "posture_attempt_count": str(self.posture_attempts),
            "last_posture_code": str(self.posture_code),
            "last_posture_reply_received": "true" if self.posture_attempts else "false",
        }
        diagnostic = types.SimpleNamespace(
            name="go2_chassis_adapter",
            message="SDK heartbeat failed" if faulted else (
                "armed and stopped" if self.armed else "disarmed"),
            values=[types.SimpleNamespace(key=k, value=v) for k, v in values.items()])
        self.callbacks["/diagnostics"](types.SimpleNamespace(status=[diagnostic]))
        now = time.time()
        seconds = 0 if os.environ.get("GO2_TEST_NATIVE") == "1" else int(now)
        message = types.SimpleNamespace(
            stamp=types.SimpleNamespace(sec=seconds, nanosec=(
                0 if seconds == 0 else int((now-seconds)*1e9))),
            mode=(int(os.environ.get("GO2_TEST_MODE","1"))
                  if os.environ.get("GO2_TEST_NATIVE") == "1" else 5),
            error_code=0, position=[0, 0, 0], velocity=[0, 0, 0],
            imu_state=types.SimpleNamespace(quaternion=[1, 0, 0, 0]))
        self.callbacks["/lf/sportmodestate"](message)
        if "/lowstate" in self.callbacks:
            self.callbacks["/lowstate"](types.SimpleNamespace(
                tick=int(time.monotonic()*1000)&0xffffffff,
                motor_state=[types.SimpleNamespace(dq=0.0) for _ in range(16)]))
        for callback in self.timers:
            callback()
        time.sleep(0.01)


node = Node()
module("rclpy", init=lambda **_: None, create_node=lambda *a, **k: node,
       ok=lambda: True, spin_once=lambda *a, **k: node.spin(), shutdown=lambda: None)
module("rclpy.qos", qos_profile_sensor_data=object())
for name, value in [
    ("diagnostic_msgs", {"DiagnosticArray": object}),
    ("geometry_msgs", {"Twist": Twist}),
    ("std_srvs", {"SetBool": SetBool, "Trigger": Trigger}),
    ("unitree_go", {"SportModeState": object, "LowState": object}),
]:
    module(name)
    module(name + (".srv" if name == "std_srvs" else ".msg"), **value)
worker = Path(__file__).resolve().parents[1] / "go2w_chassis_motion/ros_worker.py"
sys.path.insert(0, str(worker.parent))
sys.argv = [str(worker), "--allowed-modes", "5", "--allow-motion",
            "--operator-present", "--safety-ack", "I_UNDERSTAND_GO2_CAN_MOVE"]
if os.environ.get("GO2_TEST_NATIVE") == "1":
    sys.argv += ["--state-time-basis", "dds-source-lowstate-tick-v1"]
    sys.argv[2] = "1,5"
runpy.run_path(str(worker), run_name="__main__")
