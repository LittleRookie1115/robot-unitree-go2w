#!/usr/bin/env python3
"""Foxy velocity/posture bridge. Never initializes a Unitree SDK client."""
import argparse
import json
import math
import signal
import sys
import threading
import time

import rclpy
from diagnostic_msgs.msg import DiagnosticArray
from geometry_msgs.msg import Twist
try:
    from nav_msgs.msg import Odometry
except ImportError:
    Odometry = None
from rclpy.qos import qos_profile_sensor_data
from std_srvs.srv import SetBool, Trigger
from unitree_go.msg import SportModeState, LowState
from state_guard import SportWitness, NativeWitness, effective_session_enabled

COMMAND_TOPIC = "/go2/native/cmd_vel"
NATIVE_BASIS = "dds-source-lowstate-tick-v1"
NATIVE_PROFILE = "native-go2w-manual-v2"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowed-modes", required=True)
    parser.add_argument("--allow-motion", action="store_true")
    parser.add_argument("--operator-present", action="store_true")
    parser.add_argument("--safety-ack", required=True)
    parser.add_argument("--max-vx", type=float, default=0.25)
    parser.add_argument("--max-vy", type=float, default=0.10)
    parser.add_argument("--max-wz", type=float, default=0.50)
    parser.add_argument("--state-time-basis", choices=("payload", NATIVE_BASIS), default="payload")
    args = parser.parse_args()
    if not args.allow_motion or not args.operator_present or args.safety_ack != "I_UNDERSTAND_GO2_CAN_MOVE":
        raise SystemExit("Explicit motion, operator and acknowledgement flags are required")
    modes = {int(value) for value in args.allowed_modes.split(",")}
    if not modes or any(not 0 <= mode <= 254 for mode in modes):
        raise SystemExit("Invalid audited mode list")
    if not all(math.isfinite(value) and value > 0
               for value in (args.max_vx, args.max_vy, args.max_wz)):
        raise SystemExit("Invalid velocity limits")
    rclpy.init(args=[])
    node = rclpy.create_node("go2w_native_motion_bridge", enable_rosout=False,
                            start_parameter_services=False)
    publisher = node.create_publisher(Twist, COMMAND_TOPIC, 1)
    disarm = node.create_client(SetBool, "/go2_chassis/arm")
    finish_client = node.create_client(Trigger, "/go2_chassis/finish")
    finish_pose_client = node.create_client(Trigger, "/go2_chassis/finish_posture")
    poses = {action: node.create_client(Trigger, "/go2_chassis/" + action)
             for action in ("stand_up", "stand_down", "balance_stand")}
    lock = threading.RLock()
    stopping = threading.Event()
    native = args.state_time_basis == NATIVE_BASIS
    witness = NativeWitness(modes) if native else SportWitness(modes)
    state = {"diagnostic": {}, "diagnostic_at": 0.0, "deadline": 0.0,
             "active": False, "posture": False, "fault": "", "last_stop": None,
             "arming_until": 0.0, "session_enabled": False, "disarmed_locally": False,
             "motion_observation": {}, "collect_motion": False, "command": (0., 0., 0.),
             "sport_velocity": [0., 0., 0.], "sport_yaw": 0.0,
             "sport_mode": 0, "sport_stamp": 0.0,
             "slam_velocity": [0., 0., 0.], "slam_yaw": 0.0, "slam_at": 0.0,
             "wheel_dq": [0., 0., 0., 0.], "wheel_speed": 0.0, "wheel_at": 0.0,
             "feedback_sequence": 0, "fault_evidence": None, "fault_evidence_logged": False}

    def record_fault(reason):
        with lock:
            if state["fault_evidence"] is None:
                now = time.monotonic()
                diag = state["diagnostic"]
                state["fault_evidence"] = {
                    "event": "native_motion_guard_fault", "reason": reason,
                    "wall_time_sec": time.time(), "monotonic_sec": now,
                    "state_witness": witness.details(now) if native else {
                        "valid": witness.valid, "source_stamp": witness.source_stamp,
                        "sport_valid_age_sec": now-witness.received_at},
                    "adapter_diagnostic_age_sec": now-state["diagnostic_at"],
                    "adapter_state_valid": diag.get("state_valid"),
                    "adapter_guard_state": diag.get("guard_state"),
                    "adapter_clock_fault": diag.get("native_clock_fault"),
                    "command": list(state["command"]),
                    "active": state["active"], "posture": state["posture"],
                }
            state["fault"] = reason

    def current_velocity():
        sport = list(state["sport_velocity"])
        sport_valid = state["sport_stamp"] > 0.0 or any(
            abs(value) > 1e-6 for value in sport)
        if sport_valid:
            return sport, state["sport_yaw"], "sportmodestate"
        if time.monotonic() - state["slam_at"] <= 0.3:
            return list(state["slam_velocity"]), state["slam_yaw"], "slam_odom"
        return sport, state["sport_yaw"], "zero_filled"

    def update_observation():
        with lock:
            if not state["collect_motion"]:
                return
            observation = state["motion_observation"]
            velocity, yaw, source = current_velocity()
            slam_fresh = time.monotonic() - state["slam_at"] <= 0.3
            wheel_fresh = time.monotonic() - state["wheel_at"] <= 0.3
            slam = list(state["slam_velocity"]) if slam_fresh else [0., 0., 0.]
            slam_yaw = state["slam_yaw"] if slam_fresh else 0.0
            wheel_speed = state["wheel_speed"] if wheel_fresh else 0.0
            observation["samples"] += 1
            observation["last_velocity_mps"] = velocity
            observation["velocity_source"] = source
            observation["last_sport_velocity_mps"] = list(state["sport_velocity"])
            observation["last_slam_linear_mps"] = slam if slam_fresh else None
            observation["last_yaw_speed_rps"] = yaw
            observation["last_wheel_speed_rad_s"] = (
                wheel_speed if wheel_fresh else None)
            vx, vy, wz = state["command"]
            sport = state["sport_velocity"]
            sport_yaw = state["sport_yaw"]
            baseline = observation["baseline_velocity_mps"]
            sport_linear = (bool(vx or vy) and math.hypot(*sport[:2]) > .01
                            and math.hypot(sport[0]-baseline[0],
                                           sport[1]-baseline[1]) > .005)
            slam_linear = (bool(vx or vy) and slam_fresh
                           and math.hypot(*slam[:2]) > .005)
            sport_angular = (bool(wz) and abs(sport_yaw) > .02 and
                             abs(sport_yaw -
                                 observation["baseline_yaw_speed_rps"]) > .01)
            slam_angular = (bool(wz) and slam_fresh and abs(slam_yaw) > .01)
            observation["sport_linear_observed"] = (
                observation.get("sport_linear_observed", False) or sport_linear)
            observation["sport_angular_observed"] = (
                observation.get("sport_angular_observed", False) or sport_angular)
            observation["slam_linear_observed"] = (
                observation.get("slam_linear_observed", False) or slam_linear)
            observation["slam_angular_observed"] = (
                observation.get("slam_angular_observed", False) or slam_angular)
            observation["wheel_motion_observed"] = (
                bool(vx or vy) and wheel_speed > .02)
            linear = sport_linear or slam_linear
            angular = sport_angular or slam_angular
            observation["consecutive_motion_samples"] = (
                observation["consecutive_motion_samples"] + 1
                if linear or angular else 0)
            observation["motion_observed"] = (
                observation["motion_observed"] or
                observation["consecutive_motion_samples"] >= 2)

    def on_diagnostic(message):
        for item in message.status:
            if item.name == "go2_chassis_adapter":
                values = {value.key: value.value for value in item.values}
                values["adapter_reason"] = item.message
                with lock:
                    state["diagnostic"] = values
                    state["diagnostic_at"] = time.monotonic()
                    if native:
                        try:
                            witness.observe_dds(int(values.get("sport_dds_source_ns", "0")),
                                                time.monotonic())
                        except ValueError:
                            record_fault("malformed_source_clock_diagnostic")

    def on_state(message, source="primary"):
        stamp = message.stamp.sec + message.stamp.nanosec * 1e-9
        q = [float(value) for value in message.imu_state.quaternion]
        values = q + list(message.position) + list(message.velocity)
        if hasattr(message, "yaw_speed"):
            values.append(float(message.yaw_speed))
        with lock:
            if native:
                witness.observe(stamp, int(message.mode), message.error_code, q, values,
                                time.time(), time.monotonic(), source)
            else:
                witness.observe(stamp, int(message.mode), message.error_code, q, values,
                                time.time(), time.monotonic())
            if not witness.ready(time.time(), time.monotonic()):
                return
            state["feedback_sequence"] += 1
            state["sport_velocity"] = [float(value) for value in message.velocity]
            state["sport_yaw"] = float(getattr(message, "yaw_speed", 0.0))
            state["sport_mode"] = int(message.mode)
            state["sport_stamp"] = stamp
            update_observation()

    def on_low(message):
        dq = [float(motor.dq) for motor in message.motor_state]
        wheel = dq[12:16] if len(dq) >= 16 else []
        now = time.monotonic()
        with lock:
            witness.observe_tick(int(message.tick), now)
            if len(wheel) == 4:
                state["wheel_dq"] = wheel
                state["wheel_speed"] = sum(abs(value) for value in wheel) / 4.0
                state["wheel_at"] = now

    def on_slam(message):
        twist = message.twist.twist
        with lock:
            state["slam_velocity"] = [float(twist.linear.x),
                                      float(twist.linear.y),
                                      float(twist.linear.z)]
            state["slam_yaw"] = float(twist.angular.z)
            state["slam_at"] = time.monotonic()
        update_observation()

    def health_reason():
        diag = state["diagnostic"]
        now = time.monotonic()
        if state["fault"]:
            return state["fault"]
        if not witness.ready(time.time(), now):
            if native and not witness.fault:
                return witness.unready_reason(now)
            return witness.fault or "sport_state_invalid_or_stale"
        if now - state["diagnostic_at"] > 0.30:
            return "adapter_diagnostics_stale"
        if diag.get("guard_state") == "FAULT":
            return "adapter_fault: " + diag.get("adapter_reason", "unknown")
        if (diag.get("allow_motion") != "true" or diag.get("state_valid") != "true"
                or diag.get("source_stamp_status") != "fresh"
                or diag.get("motion_profile") != NATIVE_PROFILE
                or (native and diag.get("state_time_basis") != NATIVE_BASIS)):
            return "guard_state_unhealthy_or_profile_mismatch"
        if node.count_publishers(COMMAND_TOPIC) != 1 or node.count_subscribers(COMMAND_TOPIC) != 1:
            return "guarded_velocity_channel_has_missing_or_competing_owners"
        return ""

    def backend_ready():
        diag = state["diagnostic"]
        return (not health_reason() and not state["disarmed_locally"]
                and diag.get("guard_state") == "ARMED" and diag.get("daemon_armed") == "true")

    def status():
        with lock:
            velocity, yaw, source = current_velocity()
            slam_fresh = time.monotonic() - state["slam_at"] <= 0.3
            wheel_fresh = time.monotonic() - state["wheel_at"] <= 0.3
            diag = state["diagnostic"]
            daemon_armed = diag.get("daemon_armed") == "true"
            session_enabled = effective_session_enabled(state["session_enabled"], diag)
            reason = health_reason()
            if not reason and not session_enabled:
                reason = ("backend_disarmed_requires_human_enable"
                          if state["session_enabled"] else "operator_enable_required")
            if not reason and not backend_ready():
                reason = "guard_not_enabled_or_healthy"
            return {"ready": not reason, "reason": reason,
                    "faulted": bool(state["fault"]) or diag.get("guard_state") == "FAULT",
                    "adapter_reason": diag.get("adapter_reason", ""),
                    "native_clock_fault": diag.get("native_clock_fault", ""),
                    "state_witness": witness.details(time.monotonic()) if native else None,
                    "fault_evidence": state["fault_evidence"],
                    "session_enabled": session_enabled,
                    "idle_timeout_sec": None,
                    "can_arm": not health_reason() and diag.get("guard_state") == "DISARMED",
                    "guard_state": diag.get("guard_state", "unknown"),
                    "daemon_armed": daemon_armed,
                    "limits": {"max_vx": args.max_vx,
                               "max_vy": args.max_vy,
                               "max_wz": args.max_wz},
                    "source_stamp": witness.source_stamp,
                    "state_time_basis": args.state_time_basis,
                    "absolute_measurement_age_verified": False,
                    "movement_ready": not reason and (
                        not native or (diag.get("movement_mode_ready") == "true"
                                       and witness.mode in (1,3))),
                    "mode": diag.get("mode", "unknown"),
                    "dispatch": {**{name: int(diag.get(name, "0")) for name in (
                        "move_dispatch_count", "move_attempt_count", "last_move_code",
                        "move_failure_count", "stop_ack_count", "sdk_posture_dispatch_count",
                        "sdk_posture_finish_count", "posture_attempt_count",
                        "last_posture_action_id", "last_posture_code")},
                            "motion_evidence_instance": diag.get("motion_evidence_instance", ""),
                            "last_posture_reply_received":
                                diag.get("last_posture_reply_received") == "true",
                            "last_move_reply_received": diag.get("last_move_reply_received") == "true",
                            "last_move_remote_response_verified":
                                diag.get("last_move_remote_response_verified") == "true",
                            "last_move_velocity": [float(diag.get("last_move_"+axis, "0"))
                                                   for axis in ("vx", "vy", "wz")]},
                    "motion_observation": dict(state["motion_observation"]),
                    "telemetry": {
                        "sequence": state["feedback_sequence"],
                        "mode": state["sport_mode"],
                        "velocity_mps": velocity,
                        "velocity_source": source,
                        "sport_velocity_mps": list(state["sport_velocity"]),
                        "yaw_speed_rps": yaw,
                        "slam_linear_mps": (
                            list(state["slam_velocity"])
                            if slam_fresh else None),
                        "slam_yaw_speed_rps": (
                            state["slam_yaw"] if slam_fresh else None),
                        "wheel_speed_rad_s": (
                            state["wheel_speed"] if wheel_fresh else None),
                        "wheel_dq": (
                            list(state["wheel_dq"]) if wheel_fresh else None),
                    }}

    def request_stop():
        evidence = None
        with lock:
            state["active"] = False
            state["posture"] = False
            state["collect_motion"] = False
            state["deadline"] = 0.0
            state["disarmed_locally"] = True
            state["arming_until"] = 0.0
            state["session_enabled"] = False
            publisher.publish(Twist())
            future = (disarm.call_async(SetBool.Request(data=False))
                      if disarm.service_is_ready() else None)
            state["last_stop"] = future
            if state["fault_evidence"] is not None and not state["fault_evidence_logged"]:
                state["fault_evidence_logged"] = True
                evidence = state["fault_evidence"]
        # Queue zero/disarm before writing evidence; logging may be slow.
        if evidence is not None:
            try:
                print(json.dumps(evidence, allow_nan=False), file=sys.stderr, flush=True)
            except OSError:
                pass
        return future

    def stop_and_wait():
        future = request_stop()
        if future is None:
            return False
        deadline = time.monotonic() + 1.0
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            return future.done() and future.result() is not None and future.result().success
        except Exception:
            return False

    def finish_and_wait():
        with lock:
            if not state["session_enabled"] or not backend_ready():
                return False
            state["active"] = False
            state["posture"] = False
            state["deadline"] = 0.0
            publisher.publish(Twist())
            if not finish_client.service_is_ready():
                record_fault("finish_service_unavailable")
                request_stop()
                return False
            future = finish_client.call_async(Trigger.Request())
            before_stop = int(state["diagnostic"].get("stop_ack_count", "0"))
        end = time.monotonic()+1.0
        while not future.done() and time.monotonic() < end:
            time.sleep(0.01)
        try:
            acknowledged = future.done() and future.result() is not None and future.result().success
        except Exception:
            acknowledged = False
        with lock:
            if not acknowledged or not state["session_enabled"] or not backend_ready():
                record_fault(state["fault"] or "stop_unconfirmed_or_guard_lost")
                request_stop()
                return False
        # Finish publishes an authoritative adapter snapshot. Wait for it so
        # an old diagnostic frame cannot turn a dispatched command into success.
        end = time.monotonic()+.3
        while time.monotonic() < end:
            with lock:
                if int(state["diagnostic"].get("stop_ack_count", "0")) > before_stop:
                    break
            time.sleep(.005)
        with lock:
            state["collect_motion"] = False
        return True

    def finish_posture_and_wait():
        with lock:
            if not state["posture"] or not state["session_enabled"] or not backend_ready():
                return False
            if not finish_pose_client.service_is_ready():
                return False
            state["deadline"] = time.monotonic()+1.2
            future = finish_pose_client.call_async(Trigger.Request())
        end = time.monotonic()+1.0
        while not future.done() and time.monotonic() < end:
            time.sleep(.005)
        try:
            acknowledged = future.done() and future.result() is not None and future.result().success
        except Exception:
            acknowledged = False
        with lock:
            if not acknowledged or not backend_ready():
                return False
            state["active"] = state["posture"] = False
            state["deadline"] = 0.0
            publisher.publish(Twist())
        return True

    def watchdog():
        with lock:
            now = time.monotonic()
            if state["arming_until"]:
                reason = health_reason()
                if reason or now >= state["arming_until"]:
                    record_fault(reason or "arming_deadline_expired")
                    request_stop()
                else:
                    publisher.publish(Twist())
                return
            if not state["active"] and state["session_enabled"]:
                if not status()["ready"]:
                    record_fault(status()["reason"])
                    request_stop()
                else:
                    publisher.publish(Twist())
                return
            if not state["active"]:
                return
            valid = status()
            if time.monotonic() > state["deadline"] or not valid["ready"]:
                record_fault(valid["reason"] or "command_lease_expired")
                request_stop()
            elif state["posture"]:
                # Zero maintains the adapter's command freshness. This timer
                # never dispatches a posture API or renews the pipe lease.
                publisher.publish(Twist())

    subscriptions = [
        node.create_subscription(DiagnosticArray, "/diagnostics", on_diagnostic, 10),
        node.create_subscription(
            SportModeState, "/sportmodestate",
            lambda message: on_state(message, "primary"), qos_profile_sensor_data),
        node.create_subscription(
            SportModeState, "/lf/sportmodestate",
            lambda message: on_state(message, "fallback"), qos_profile_sensor_data),
    ]
    if Odometry is not None:
        subscriptions.append(node.create_subscription(
            Odometry, "/slam/odom", on_slam, qos_profile_sensor_data))
    if native:
        subscriptions.append(node.create_subscription(
            LowState, "/lowstate", on_low, qos_profile_sensor_data))
    timer = node.create_timer(0.02, watchdog)

    def spin():
        while not stopping.is_set() and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.02)

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    signal.signal(signal.SIGTERM, lambda *_: (request_stop(), stopping.set()))
    signal.signal(signal.SIGINT, lambda *_: (request_stop(), stopping.set()))
    try:
        while not stopping.is_set():
            line = sys.stdin.buffer.readline(16385)
            if not line:
                break
            request = {}
            try:
                if len(line) > 16384:
                    raise ValueError("request exceeds the bounded pipe message size")
                request = json.loads(line)
                operation = request.get("operation")
                if operation == "status":
                    response = {"status": status()}
                elif operation in ("enable", "arm"):
                    with lock:
                        valid = status()
                        if valid["ready"] and state["session_enabled"]:
                            print(json.dumps({"id": request.get("id"), "ok": True,
                                              "armed": True, "session_enabled": True,
                                              "idle_window_sec": None, "status": valid}), flush=True)
                            continue
                        if state["active"] or state["arming_until"] or not valid["can_arm"]:
                            raise RuntimeError(valid["reason"] or "explicit disarmed healthy state is required")
                        if not disarm.service_is_ready():
                            raise RuntimeError("native arm service is unavailable")
                        state["fault_evidence"] = None
                        state["fault_evidence_logged"] = False
                        state["arming_until"] = time.monotonic()+3.5
                        publisher.publish(Twist())
                    # Human enable is the authorization. The zero preparation
                    # and internal SetBool arm remain implementation details.
                    time.sleep(0.25)
                    with lock:
                        if state["fault"] or not state["arming_until"] or stopping.is_set():
                            raise RuntimeError(state["fault"] or "arm_cancelled")
                        future = disarm.call_async(SetBool.Request(data=True))
                    end = time.monotonic()+1.0
                    while not future.done() and time.monotonic() < end:
                        time.sleep(0.01)
                    if not future.done() or future.result() is None or not future.result().success:
                        raise RuntimeError("explicit arm service rejected or timed out")
                    with lock:
                        state["disarmed_locally"] = False
                    end = time.monotonic()+2.0
                    while time.monotonic() < end and not backend_ready():
                        if state["fault"] or stopping.is_set():
                            break
                        time.sleep(0.01)
                    with lock:
                        if not backend_ready():
                            raise RuntimeError(status()["reason"])
                        state["arming_until"] = 0.0
                        state["session_enabled"] = True
                    response = {"armed": True, "session_enabled": True,
                                "idle_window_sec": None, "status": status()}
                elif operation == "stop":
                    acknowledged = stop_and_wait()
                    if acknowledged:
                        with lock:
                            diag = state["diagnostic"]
                            recoverable = {
                                "guard_state_unhealthy_or_profile_mismatch",
                                "sport_state_invalid_or_stale",
                                "sport_state_stale",
                                "sport_payload_invalid",
                                "lowstate_tick_stale",
                                "dds_progress_stale",
                                "adapter_diagnostics_stale",
                                "stop_unconfirmed_or_guard_lost",
                                "command_lease_expired",
                            }
                            if (state["fault"] in recoverable
                                    and diag.get("guard_state") != "FAULT"):
                                state["fault"] = ""
                    response = {"stop_acknowledged": acknowledged}
                elif operation == "finish":
                    response = {"stop_acknowledged": finish_and_wait(),
                                "session_enabled": state["session_enabled"]}
                elif operation == "finish_posture":
                    response = {"action_finished": finish_posture_and_wait(),
                                "session_enabled": state["session_enabled"]}
                elif operation == "posture":
                    action = request.get("action")
                    if action not in poses:
                        raise ValueError("unsupported native posture request")
                    with lock:
                        valid = status()
                        if not valid["ready"] or state["active"]:
                            raise RuntimeError(valid["reason"] or "another command is active")
                        client = poses[action]
                        if not client.service_is_ready():
                            raise RuntimeError("native posture service is unavailable")
                        state["active"], state["posture"] = True, True
                        state["deadline"] = time.monotonic() + 0.25
                        publisher.publish(Twist())
                        future = client.call_async(Trigger.Request())
                    deadline = time.monotonic() + 0.4
                    while not future.done() and time.monotonic() < deadline:
                        time.sleep(0.005)
                    if not future.done():
                        raise TimeoutError("native posture service response timed out")
                    posture_reply = future.result()
                    if posture_reply is None:
                        raise RuntimeError("native posture service returned no response")
                    if not posture_reply.success:
                        raise RuntimeError("native posture request failed: " +
                                           (posture_reply.message or "no error detail"))
                    with lock:
                        if state["fault"]:
                            raise RuntimeError(state["fault"])
                        state["deadline"] = time.monotonic() + 0.15
                    response = {"dispatch_acknowledged": True}
                elif operation == "posture_hold":
                    with lock:
                        valid = status()
                        if not valid["ready"] or not state["active"] or not state["posture"]:
                            raise RuntimeError(valid["reason"] or "no posture is active")
                        state["deadline"] = time.monotonic() + 0.15
                    response = {"accepted": True}
                elif operation == "move":
                    vx, vy, wz = (float(request[key]) for key in ("vx", "vy", "wz"))
                    if not all(math.isfinite(v) for v in (vx, vy, wz)) or not (
                            abs(vx) <= args.max_vx
                            and abs(vy) <= args.max_vy
                            and abs(wz) <= args.max_wz):
                        raise ValueError("velocity exceeds the native commissioning envelope")
                    with lock:
                        valid = status()
                        if not valid["ready"]:
                            raise RuntimeError(valid["reason"])
                        if (vx or vy or wz) and not valid["movement_ready"]:
                            raise RuntimeError("robot_mode_not_ready_for_velocity_check_balance_stand")
                        if state["posture"]:
                            raise RuntimeError("velocity may not overlap an active posture")
                        twist = Twist()
                        twist.linear.x, twist.linear.y, twist.angular.z = vx, vy, wz
                        if not state["active"]:
                            velocity, yaw, source = current_velocity()
                            slam_fresh = (
                                time.monotonic() - state["slam_at"] <= 0.3)
                            state["motion_observation"] = {
                                "samples": 0, "motion_observed": False,
                                "basis": "sport_velocity_with_slam_odom_fallback",
                                "velocity_source": source,
                                "absolute_measurement_age_verified": False,
                                "direction_verified": False,
                                "baseline_velocity_mps": velocity,
                                "baseline_yaw_speed_rps": yaw,
                                "baseline_slam_linear_mps": (
                                    list(state["slam_velocity"])
                                    if slam_fresh else [0., 0., 0.]),
                                "baseline_slam_yaw_speed_rps": (
                                    state["slam_yaw"] if slam_fresh else 0.0),
                                "consecutive_motion_samples": 0,
                                "minimum_linear_speed_mps": .01,
                                "minimum_slam_speed_mps": .005,
                                "minimum_yaw_speed_rps": .02,
                                "sport_linear_observed": False,
                                "sport_angular_observed": False,
                                "slam_linear_observed": False,
                                "slam_angular_observed": False,
                                "wheel_motion_observed": False,
                            }
                        state["collect_motion"] = True
                        state["command"] = (vx, vy, wz)
                        state["deadline"] = time.monotonic() + 0.15
                        state["active"] = True
                        publisher.publish(twist)
                    response = {"accepted": True, "accepted_by": "ros_worker",
                                "firmware_acceptance_pending": True}
                else:
                    raise ValueError("unknown motion-bridge operation")
                reply = {"id": request.get("id"), "ok": True, **response}
            except Exception as error:
                with lock:
                    if state["active"] or state["arming_until"]:
                        record_fault(str(error))
                        request_stop()
                reply = {"id": request.get("id"), "ok": False, "error": str(error)}
            print(json.dumps(reply), flush=True)
    finally:
        stop_and_wait()
        stopping.set()
        thread.join(timeout=2)
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
