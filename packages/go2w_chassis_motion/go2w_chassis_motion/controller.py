"""ROS/SDK-independent bounded command handling; tests use an in-memory transport."""
from dataclasses import dataclass, asdict
import json
import math
from pathlib import Path
import threading
import time


class MotionError(ValueError):
    pass


LIMITS_PATH = Path.home() / ".config/robonix/motion_limits.json"

# Absolute hard ceilings. The runtime limits file may only tighten these.
HARD_LIMITS = {
    "forward_mps": 1.0,
    "reverse_mps": 1.0,
    "lateral_mps": 0.5,
    "yaw_rps": 1.0,
    "default_duration_s": 3.0,
    "max_duration_s": 3.0,
    "period_s": 0.05,
    "posture_duration_s": 4.0,
}

# Conservative defaults used when the runtime limits file is absent.
DEFAULT_LIMITS = {
    "forward_mps": 0.25,
    "reverse_mps": 0.25,
    "lateral_mps": 0.10,
    "yaw_rps": 0.50,
    "default_duration_s": 0.50,
    "max_duration_s": 2.0,
    "period_s": 0.05,
    "posture_duration_s": 4.0,
}


@dataclass(frozen=True)
class Limits:
    forward_mps: float = 0.25
    reverse_mps: float = 0.25
    lateral_mps: float = 0.10
    yaw_rps: float = 0.50
    default_duration_s: float = 0.50
    max_duration_s: float = 2.0
    period_s: float = 0.05
    posture_duration_s: float = 4.0

    def __post_init__(self):
        ceilings = dict(HARD_LIMITS)
        ceilings["default_duration_s"] = HARD_LIMITS["max_duration_s"]
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= ceiling:
                raise MotionError(f"{name} must be finite in (0, {ceiling}]")
        if self.default_duration_s > self.max_duration_s:
            raise MotionError("default duration exceeds the maximum")


def load_limits(path=None, fallback=None):
    target = Path(path) if path is not None else LIMITS_PATH
    base = fallback or Limits()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return base
    except OSError as error:
        raise MotionError(f"cannot read motion limits: {error}") from error
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise MotionError(f"invalid motion limits JSON: {error}") from error
    if not isinstance(data, dict):
        raise MotionError("motion limits file must contain an object")
    merged = {**asdict(base), **data}
    try:
        return Limits(**merged)
    except TypeError as error:
        raise MotionError(f"invalid motion limits field: {error}") from error


@dataclass(frozen=True)
class Burst:
    vx: float
    vy: float
    wz: float
    duration_s: float
    mode: str


def plan_command(command, limits):
    names = ("linear_x", "linear_y", "linear_z", "angular_x", "angular_y", "angular_z",
             "duration_sec", "forward_m", "rotate_deg")
    values = {name: float(getattr(command, name, 0.0)) for name in names}
    if not all(math.isfinite(value) for value in values.values()):
        raise MotionError("all command fields must be finite")
    if values["duration_sec"] < 0:
        raise MotionError("duration_sec must not be negative")
    if any(values[name] != 0 for name in ("linear_z", "angular_x", "angular_y")):
        raise MotionError("the planar Go2 envelope rejects vertical, roll and pitch motion")
    if values["forward_m"] != 0:
        speed = limits.forward_mps if values["forward_m"] > 0 else -limits.reverse_mps
        burst = Burst(speed, 0.0, 0.0, abs(values["forward_m"] / speed), "forward_m")
    elif values["rotate_deg"] != 0:
        angle = math.radians(values["rotate_deg"])
        burst = Burst(0.0, 0.0, math.copysign(limits.yaw_rps, angle),
                      abs(angle) / limits.yaw_rps, "rotate_deg")
    else:
        if not -limits.reverse_mps <= values["linear_x"] <= limits.forward_mps:
            raise MotionError("linear_x exceeds the forward/reverse speed limit")
        if abs(values["linear_y"]) > limits.lateral_mps:
            raise MotionError("linear_y exceeds the lateral speed limit")
        if abs(values["angular_z"]) > limits.yaw_rps:
            raise MotionError("angular_z exceeds the yaw limit")
        duration = values["duration_sec"] or limits.default_duration_s
        burst = Burst(values["linear_x"], values["linear_y"], values["angular_z"], duration, "velocity")
    if not 0 < burst.duration_s <= limits.max_duration_s:
        raise MotionError(f"movement duration must be in (0, {limits.max_duration_s}]")
    return burst


POSTURE_TARGET_MODES = {"stand_down": 5, "stand_up": 6, "balance_stand": 1}
POSTURE_DURATION_S = {"stand_down": 3.0, "stand_up": 4.0, "balance_stand": 2.0}


def posture_mode(value):
    try:
        mode = int(value)
    except (TypeError, ValueError):
        return None
    return mode if 0 <= mode <= 255 else None


def posture_already_reached(action, mode):
    return mode is not None and POSTURE_TARGET_MODES.get(action) == mode


def posture_transition_supported(action, mode):
    # Mode 1 is BalanceStand, not the locked StandUp mode 6. It must be able
    # to transition to mode 6; otherwise the robot can be stuck standing in
    # mode 1 where the firmware does not accept StandDown directly.
    # Mode 3 is treated as already standing; unknown modes stay dispatchable.
    if mode is None:
        return True
    if action == "stand_up":
        return mode != 3
    if action == "balance_stand":
        return mode != 5
    if action == "stand_down":
        return mode != 1
    return True


def posture_recommendation(action, mode):
    if action == "stand_up" and mode == 3:
        return "already_standing"
    if action == "balance_stand" and mode == 5:
        return "stand_up_first"
    if action == "stand_down" and mode == 1:
        return "stand_up_first"
    if action == "stand_down" and mode == 5:
        return "already_lying"
    return ""


class MotionController:
    def __init__(self, transport=None, limits=None, transport_factory=None):
        self.transport = transport
        self._transport_factory = transport_factory
        self._disabling = False
        self.limits = limits or Limits()
        self._lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._cancel = None
        self._closed = False
        self._faulted = False
        self._last = {"status": "idle"}

    def status(self):
        with self._lock:
            last = dict(self._last)
            result = {"motion_enabled": self.transport is not None,
                      "active": self._cancel is not None,
                      "closed": self._closed, "faulted": self._faulted,
                      "backend_owner": (getattr(self.transport, "backend_owner", "external")
                                        if self.transport is not None else None),
                      "limits": asdict(self.limits),
                      "session_enabled": None, "guard_ready": None,
                      "guard_reason": None, "guard_state": None,
                      "mode": None, "daemon_armed": None}
        if self.transport is not None:
            try:
                result["guard"] = self.transport.status()
                result["faulted"] = result["faulted"] or bool(result["guard"].get("faulted"))
            except Exception as error:
                result["guard"] = {"ready": False, "reason": str(error)}
                result["faulted"] = True
        else:
            result["guard"] = {"ready": False, "reason": "motion_disabled"}
        guard = result["guard"]
        result["session_enabled"] = bool(guard.get("session_enabled"))
        result["guard_ready"] = bool(guard.get("ready"))
        result["guard_reason"] = guard.get("reason")
        result["guard_state"] = guard.get("guard_state")
        result["mode"] = guard.get("mode")
        result["daemon_armed"] = guard.get("daemon_armed")
        result["last_result"] = last
        return result

    def stop(self, disarm=False, clear_fault=False):
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()
        result = self._stop_backend(disarm=disarm, clear_fault=clear_fault)
        with self._lock:
            self._last = dict(result)
        return result

    def _stop_backend(self, disarm=False, clear_fault=False):
        """Stop hardware without cancelling a replacement authorization request."""
        if self.transport is None:
            return {"status": "disabled", "stop_acknowledged": False,
                    "detail": "No motion transport exists; no hardware stop was sent."}
        try:
            revoke = disarm or self._faulted or self._closed or self._disabling
            acknowledged = self.transport.stop() if revoke else self.transport.finish()
        except Exception as error:
            acknowledged = False
            detail = str(error)
        else:
            detail = ("Guarded stop/disarm acknowledged" if revoke else
                      "StopMove acknowledged; operator enable retained") if acknowledged else "Stop unconfirmed"
        if not acknowledged:
            with self._lock:
                self._faulted = True
            # A normal StopMove failure must also revoke session permission.
            try:
                self.transport.stop()
            except Exception:
                pass
        elif clear_fault and self._faulted:
            # An explicit human stop may clear a recoverable stop/watchdog
            # fault, but never a backend FAULT.
            healthy = False
            try:
                guard = self.transport.status()
                healthy = (guard.get("faulted") is not True
                           and guard.get("guard_state") != "FAULT")
            except Exception:
                healthy = False
            if healthy:
                with self._lock:
                    self._faulted = False
                detail += "; recoverable controller fault cleared"
        return {"status": "stopped" if acknowledged else "stop_unconfirmed",
                "stop_acknowledged": acknowledged, "detail": detail}

    def disable(self):
        """Revoke immediately, then release this provider's backend ownership."""
        with self._lock:
            self._disabling = True
            if self._cancel is not None:
                self._cancel.set()
            previous = self.transport
        stopped = self.stop(disarm=True) if previous is not None else None
        if not self._execution_lock.acquire(timeout=10):
            with self._lock:
                self._faulted = True
            return {"status": "stop_unconfirmed", "session_enabled": False,
                    "stop_acknowledged": False, "error": "backend_shutdown_pending"}
        try:
            current = self.transport
            if current is not None and current is not previous:
                stopped = self.stop(disarm=True)
            acknowledged = stopped is not None and stopped["stop_acknowledged"]
            if current is not None and hasattr(current, "close"):
                current.close()
            with self._lock:
                self.transport = None
                self._faulted = stopped is not None and not acknowledged
                self._last = {
                    "status": "stop_unconfirmed" if self._faulted else "disabled",
                    "session_enabled": False,
                    "stop_acknowledged": acknowledged,
                    "hardware_stop_requested": stopped is not None,
                }
                return dict(self._last)
        finally:
            with self._lock:
                self._disabling = False
            self._execution_lock.release()

    def execute(self, command, is_active=lambda: True):
        return self._run(command=command, is_active=is_active)

    def arm(self, is_active=lambda: True):
        """Compatibility alias; operator enable is the normal public entry."""
        result = self.enable(is_active=is_active)
        if result.get("status") == "enabled":
            result["status"] = "armed"
        return result

    def reload_limits(self, path=None):
        limits = load_limits(path, fallback=self.limits)
        with self._lock:
            self.limits = limits
        return limits

    def enable(self, is_active=lambda: True):
        if not self._execution_lock.acquire(blocking=False):
            return {"status": "rejected", "error": "another_move_is_active"}
        cancel = threading.Event()
        result = None
        try:
            try:
                self.reload_limits()
            except MotionError as error:
                result = {"status": "rejected", "error": str(error)}
                return result
            with self._lock:
                if self.transport is None and self._transport_factory is None:
                    result = {"status": "rejected", "error": "motion_disabled"}
                    return result
                if self._closed or self._disabling:
                    result = {"status": "rejected", "error": "controller_closed_or_faulted"}
                    return result
                self._cancel = cancel
            if self.transport is not None:
                try:
                    backend_fault = self.transport.status().get("faulted", False)
                except Exception:
                    backend_fault = True
                self._faulted = self._faulted or backend_fault
            if self._faulted:
                if self._transport_factory is None:
                    result = {"status": "rejected", "error": "controller_closed_or_faulted"}
                    return result
                # Releasing the old backend belongs to this human enable.
                # Public stop() would also cancel this new request itself.
                self._stop_backend(disarm=True)
                if self.transport is not None:
                    self.transport.close()
                self.transport = None
                self._faulted = False
            if self.transport is None:
                self.transport = self._transport_factory()
            if cancel.is_set() or self._closed or self._disabling or not is_active():
                stopped = self.stop(disarm=True)
                result = {"status": "cancelled" if stopped["stop_acknowledged"] else "stop_unconfirmed",
                          "stop_acknowledged": stopped["stop_acknowledged"]}
                return result
            guard = self.transport.status()
            if guard.get("ready") and guard.get("session_enabled"):
                result = {"status": "enabled", "session_enabled": True, "guard": guard}
                return result
            if not guard.get("can_arm"):
                result = {"status": "rejected", "error": guard.get("reason") or "guard_not_ready_to_arm"}
                return result
            enabled = self.transport.enable()
            if cancel.is_set() or not is_active():
                stopped = self.stop(disarm=True)
                result = {"status": "cancelled" if stopped["stop_acknowledged"] else "stop_unconfirmed",
                          "stop_acknowledged": stopped["stop_acknowledged"]}
                return result
            result = {"status": "enabled", "session_enabled": True,
                      "idle_timeout_sec": None, "guard": enabled["status"]}
            return result
        except Exception as error:
            with self._lock:
                self._faulted = True
            stopped = self.stop(disarm=True)
            result = {"status": "fault", "error": str(error),
                      "stop_acknowledged": stopped["stop_acknowledged"]}
            return result
        finally:
            with self._lock:
                if result is not None:
                    self._last = dict(result)
                self._cancel = None
            self._execution_lock.release()

    def posture(self, action, is_active=lambda: True):
        return self._run(action=action, is_active=is_active)

    def _posture_precheck(self, action, guard):
        # A posture that is already held is a successful no-op, not a reason
        # to send the same firmware action again. An unambiguously invalid
        # transition is rejected without touching the session authorization.
        current_mode = posture_mode(guard.get("mode"))
        target_mode = POSTURE_TARGET_MODES[action]
        if posture_already_reached(action, current_mode):
            return {
                "status": "completed",
                "action": action,
                "action_finished": True,
                "already_in_target_mode": True,
                "dispatched": False,
                "current_mode": current_mode,
                "target_mode": target_mode,
                "stop_acknowledged": False,
                "session_enabled": bool(guard.get("session_enabled")),
            }
        if not posture_transition_supported(action, current_mode):
            return {
                "status": "rejected",
                "error": "posture_transition_not_applicable",
                "action": action,
                "current_mode": current_mode,
                "target_mode": target_mode,
                "recommended_action": posture_recommendation(action, current_mode),
                "dispatched": False,
                "session_enabled": bool(guard.get("session_enabled")),
            }
        return None

    def _run(self, command=None, action=None, is_active=lambda: True):
        if not self._execution_lock.acquire(blocking=False):
            return {"status": "rejected", "error": "another_move_is_active"}
        cancel = threading.Event()
        attempted = False
        revoke_on_finish = False
        result = None
        current_mode = None
        target_mode = None
        try:
            with self._lock:
                if self._closed or self._faulted or self._disabling:
                    result = {"status": "rejected",
                              "error": "controller_closed_or_faulted"}
                    return result
                if self.transport is None:
                    result = {"status": "rejected", "error": "motion_disabled"}
                    return result
                self._cancel = cancel
            try:
                if action is not None and action not in ("stand_up", "stand_down", "balance_stand"):
                    raise MotionError("unsupported native posture")
                burst = (plan_command(command, self.limits) if action is None else
                         Burst(0.0, 0.0, 0.0, min(
                             self.limits.posture_duration_s,
                             POSTURE_DURATION_S[action]), action))
            except (ValueError, TypeError, OverflowError) as error:
                result = {"status": "rejected", "error": str(error)}
                return result
            guard = self.transport.status()
            if not guard.get("ready"):
                result = {"status": "rejected",
                          "error": guard.get("reason", "guard_not_ready")}
                return result
            if action is not None:
                current_mode = posture_mode(guard.get("mode"))
                target_mode = POSTURE_TARGET_MODES[action]
                early = self._posture_precheck(action, guard)
                if early is not None:
                    result = early
                    return result
            if (action is None and (burst.vx or burst.vy or burst.wz) and
                    guard.get("movement_ready") is False):
                mode = str(guard.get("mode", "unknown"))
                result = {"status": "rejected",
                          "error": "robot_mode_not_ready_for_velocity",
                          "mode": mode,
                          "required_sequence": (["balance_stand"] if mode == "6" else
                                                ["stand_up", "balance_stand"] if mode == "5" else [])}
                return result
            started = time.monotonic()
            deadline = started + burst.duration_s
            dispatched = False
            while time.monotonic() < deadline:
                with self._lock:
                    context_active = is_active()
                    if cancel.is_set() or not context_active:
                        revoke_on_finish = not context_active
                        result = {"status": "cancelled"}
                        break
                    attempted = True
                    if action is None:
                        self.transport.send(burst.vx, burst.vy, burst.wz)
                    elif not dispatched:
                        self.transport.posture(action)
                        dispatched = True
                    else:
                        self.transport.hold_posture()
                cancel.wait(min(self.limits.period_s, max(0.0, deadline - time.monotonic())))
            else:
                terminal_guard = self.transport.status()
                terminal_mode = posture_mode(terminal_guard.get("mode"))
                target_reached = (action is not None and target_mode is not None
                                  and terminal_mode == target_mode)
                if not terminal_guard.get("ready") and not target_reached:
                    raise RuntimeError(terminal_guard.get("reason", "guard_lost_at_completion"))
                result = {"status": "dispatched", "mode": burst.mode,
                          "duration_s": time.monotonic() - started,
                          "completion": "timed_request_window",
                          "requested_velocity": {"vx": burst.vx, "vy": burst.vy, "wz": burst.wz},
                          "direction_verified": False,
                          "distance_verified": False, "angle_verified": False}
                if action is not None:
                    result.update(status="dispatched",
                                  completion=("target_mode_reached" if target_reached
                                              else "bounded_posture_window"),
                                  physical_completion_verified=False,
                                  terminal_mode_reached=target_reached,
                                  current_mode=current_mode, target_mode=target_mode,
                                  terminal_mode=terminal_mode)
        except Exception as error:
            with self._lock:
                self._faulted = True
            result = {"status": "fault", "error": str(error)}
            if action is not None:
                result["current_mode"] = current_mode
                result["target_mode"] = target_mode
            try:
                failure_guard = self.transport.status()
                result["dispatch"] = failure_guard.get("dispatch", {})
                result["feedback"] = failure_guard.get("motion_observation", {})
            except Exception:
                pass
        finally:
            if attempted:
                if not is_active():
                    revoke_on_finish = True
                    if result is not None and result.get("status") in ("completed", "dispatched"):
                        result["status"] = "cancelled"
                normal_posture = (action is not None and result is not None
                                  and result.get("status") == "dispatched"
                                  and not (revoke_on_finish or self._faulted or self._closed
                                           or self._disabling))
                if normal_posture:
                    finish_error = None
                    try:
                        finished = self.transport.finish_posture()
                    except Exception as error:
                        finished = False
                        finish_error = str(error)
                    if finished:
                        result["action_finished"] = True
                        result["completion"] = "controller_terminal_mode"
                        stopped = {"stop_acknowledged": True,
                                   "detail": "Posture terminal mode reached"}
                    else:
                        final_mode = None
                        try:
                            final_mode = posture_mode(self.transport.status().get("mode"))
                        except Exception:
                            pass
                        if action is not None and final_mode == target_mode:
                            # The firmware reached the target mode even though
                            # the finish RPC failed. Report the physical result
                            # and revoke the session instead of latching a
                            # false fault.
                            result["action_finished"] = True
                            result["terminal_mode_reached"] = True
                            result["completion"] = "target_mode_reached_without_finish"
                            result["finish_error"] = (finish_error or
                                                      result.pop("error", None) or
                                                      "finish_posture_failed")
                            result.pop("error", None)
                            stopped = self.stop(disarm=True)
                        else:
                            if finish_error:
                                result["error"] = finish_error
                            self._faulted = True
                            result["status"] = "fault"
                            stopped = self.stop(disarm=True)
                else:
                    stopped = self.stop(disarm=revoke_on_finish or self._faulted or self._closed)
                if result is None:
                    result = {"status": "fault"}
                result["stop_acknowledged"] = stopped["stop_acknowledged"]
                if not stopped["stop_acknowledged"] and not result.get("action_finished"):
                    result["status"] = "stop_unconfirmed"
                elif action is None and result.get("status") == "dispatched":
                    if not (burst.vx or burst.vy or burst.wz):
                        result.update(status="completed", completion="zero_velocity_window",
                                      dispatch_verified=False, physical_motion_observed=False)
                    else:
                        try:
                            final = self.transport.status()
                            before = int(guard.get("dispatch", {}).get("move_dispatch_count", 0))
                            after = int(final.get("dispatch", {}).get("move_dispatch_count", 0))
                            count = max(0, after - before)
                            instance = guard.get("dispatch", {}).get("motion_evidence_instance")
                            same_instance = bool(instance) and (
                                instance == final.get("dispatch", {}).get("motion_evidence_instance"))
                            if not same_instance:
                                count = 0
                            observation = final.get("motion_observation", {})
                            observed = bool(observation.get("motion_observed"))
                            result.update(
                                status=("completed" if observed and count else
                                        "not_observed" if count else "not_dispatched"),
                                dispatch_verified=count > 0, dispatch_count=count,
                                physical_motion_observed=observed,
                                dispatch=final.get("dispatch", {}),
                                feedback=observation,
                                completion="stopped_after_timed_request")
                        except Exception as error:
                            self._faulted = True
                            result.update(status="fault", error=str(error))
                            self.stop(disarm=True)
            with self._lock:
                if result is not None:
                    self._last = dict(result)
                self._cancel = None
            self._execution_lock.release()
        return result

    def close(self):
        with self._lock:
            self._closed = True
        return self.disable()
