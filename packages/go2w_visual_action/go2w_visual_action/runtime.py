import json
import threading
import time
from uuid import UUID, uuid4

from .robot import publish_status


TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


# No usable distance this round: skip the step and look again. These are not risks seen in
# the scene, so they do not end the task; an actual lidar_blocked still does.
# A sign or a person can go away, so they only pause the task, which resumes after a few clear
# rounds. A model-reported obstacle and a lidar block stay latched until the task is cancelled.
SIGN_REASON = "prohibition_or_uncertain"
PERSON_REASON = "person_or_uncertain"
# One reading of a sign is noisy, so the robot resumes on the average of the last few rounds.
# Measured at 2.7 m: the "no entry" sign averages 0.50, the "welcome" sign 0.27.
RESUME_ROUNDS = 3
RESUME_PROBABILITY = 0.35
RETRY_REASONS = {"lidar_unavailable", "lidar_stale", "lidar_no_return", SIGN_REASON, PERSON_REASON}


def latch_risk(blocked_reason, decision):
    if (blocked_reason is None and not decision["forward_allowed"]
            and decision["reason"] not in RETRY_REASONS):
        return decision["reason"]
    return blocked_reason


def track_sign(recent, decision):
    """Latest sign scores while a sign holds the robot. None means nothing is holding it."""
    score = decision["prohibition_probability"]
    if recent is None:
        if decision["reason"] == SIGN_REASON:
            return [score]
        return [] if decision["reason"] == PERSON_REASON else None
    if not decision["forward_allowed"] and decision["reason"] != SIGN_REASON:
        return []  # someone is in view, for example changing the sign
    recent = (recent + [score])[-RESUME_ROUNDS:]
    if len(recent) == RESUME_ROUNDS and sum(recent) / RESUME_ROUNDS < RESUME_PROBABILITY:
        return None
    return recent


class Runtime:
    def __init__(self, settings, robot):
        self.settings = settings
        self.robot = robot
        self.lock = threading.RLock()
        self.records = {}
        self.active_run = None
        self.cancelled = None
        self.worker = None
        self.closed = False

    def evaluate(self, request):
        request_id = str(UUID(request.request_id)) if request.request_id else str(uuid4())
        return self.robot.evaluate(request_id)

    def start(self, request):
        request_id = str(UUID(request.request_id)) if request.request_id else str(uuid4())
        with self.lock:
            if self.closed:
                raise RuntimeError("service is shutting down")
            if self.active_run is not None:
                raise RuntimeError("another run_step task is active")
            run_id = str(uuid4())
            record = {
                "run_id": run_id,
                "request_id": request_id,
                "state": "ACCEPTED",
                "error_code": "",
                "error_message": "",
                "motion_requested": False,
                "stop_requested": False,
                "stop_confirmed": False,
                "result": None,
                "created_unix_ns": time.time_ns(),
            }
            self.records[run_id] = record
            self.active_run = run_id
            cancelled = threading.Event()
            self.cancelled = cancelled
            self.worker = threading.Thread(
                target=self._run,
                args=(record, cancelled),
                name="go2w-visual-action",
                daemon=True,
            )
            self.worker.start()
            return dict(record)

    def _update(self, run_id, **values):
        with self.lock:
            self.records[run_id].update(values)
            publish_status("/tmp/go2w_run.json", self.records[run_id])

    def _run(self, record, cancelled):
        run_id = record["run_id"]
        self.robot.last_execution = {
            "status": "no_motion",
            "stop_requested": False,
            "stop_confirmed": False,
        }
        try:
            cycle = 0
            blocked_reason = None
            sign_clear_rounds = None
            while not cancelled.is_set():
                started = time.monotonic()
                cycle += 1
                self._update(run_id, state="OBSERVING", motion_requested=False)
                try:
                    decision = self.robot.evaluate(str(uuid4()))
                except RuntimeError as error:
                    if "CAMERA_STALE" not in str(error):
                        raise
                    # One late frame only costs this round; the robot is standing still.
                    self._update(run_id, state="WAITING", motion_requested=False)
                    cancelled.wait(0.2)
                    continue
                if cancelled.is_set():
                    raise RuntimeError("TASK_CANCELLED")
                sign_clear_rounds = track_sign(sign_clear_rounds, decision)
                # While a sign already holds the robot, a person walking up to change it must
                # not end the task. A person appearing while it is moving still latches.
                if sign_clear_rounds is None:
                    blocked_reason = latch_risk(blocked_reason, decision)
                allowed = decision["forward_allowed"] and sign_clear_rounds is None
                reason = blocked_reason or decision["reason"]
                if blocked_reason is None and decision["forward_allowed"] and not allowed:
                    reason = "waiting_sign_clear"
                if blocked_reason is None and allowed and not self.settings.motion_enabled:
                    reason = "motion_disabled"
                camera_age_s = (time.time_ns() - decision["camera_source_stamp_ns"]) / 1e9
                if blocked_reason is None and allowed and self.settings.motion_enabled:
                    if camera_age_s < 0 or camera_age_s > self.settings.decision_age_limit_s:
                        reason = "camera_stale_before_motion"
                    else:
                        self.robot.validate_motion_session()
                        if cancelled.is_set():
                            raise RuntimeError("TASK_CANCELLED")
                        camera_age_s = (time.time_ns() - decision["camera_source_stamp_ns"]) / 1e9
                        if camera_age_s < 0 or camera_age_s > self.settings.decision_age_limit_s:
                            reason = "camera_stale_before_motion"
                        else:
                            self._update(run_id, state="EXECUTING", motion_requested=True,
                                         result={"decision": decision, "action": "FORWARD", "cycle": cycle})
                            execution = self.robot.execute_forward(cancelled)
                            if cancelled.is_set():
                                raise RuntimeError("TASK_CANCELLED")
                            self._update(run_id, stop_requested=True, stop_confirmed=True,
                                         result={"decision": decision, "action": "FORWARD",
                                                 "execution": execution, "cycle": cycle,
                                                 "service_stop": self.robot.last_execution.get("stop_result")})
                            reason = None
                if reason is not None:
                    self._update(run_id, state="WAITING", motion_requested=False,
                                 result={"decision": decision, "action": "STOP",
                                         "reason": reason, "cycle": cycle,
                                         "blocked_reason": blocked_reason})
                else:
                    self._update(run_id, state="WAITING")
                cancelled.wait(max(0.0, self.settings.evaluation_interval_s -
                                   (time.monotonic() - started)))
            raise RuntimeError("TASK_CANCELLED")
        except Exception as error:
            execution = dict(self.robot.last_execution)
            state = "CANCELLED" if cancelled.is_set() else "FAILED"
            previous_result = self.status(run_id)["result"]
            self._update(
                run_id,
                state=state,
                error_code=self.error_code(error),
                error_message=str(error),
                stop_requested=execution.get("stop_requested", False),
                stop_confirmed=execution.get("stop_confirmed", False),
                result={"execution": execution, "last_result": previous_result},
            )
        finally:
            with self.lock:
                if self.active_run == run_id:
                    self.active_run = None
                if self.cancelled is cancelled:
                    self.cancelled = None

    @staticmethod
    def error_code(error):
        message = str(error)
        for code in (
            "CAMERA_STALE", "CAMERA_FRAME_INVALID", "CAMERA_INVALID",
            "CAMERA_UNAVAILABLE", "INVALID_MODEL_RESPONSE", "MOTION_NOT_ENABLED",
            "STOP_UNCONFIRMED", "TASK_CANCELLED",
        ):
            if code in message:
                return code
        if "MODEL" in message or "HTTP" in message:
            return "MODEL_UNAVAILABLE"
        if "grpc" in message.lower() or "rpc" in message.lower():
            return "ROBONIX_UNAVAILABLE"
        return "EXECUTION_FAILED"

    def status(self, run_id):
        run_id = str(UUID(run_id))
        with self.lock:
            return json.loads(json.dumps(self.records[run_id], allow_nan=False))

    def cancel(self, run_id):
        run_id = str(UUID(run_id))
        with self.lock:
            record = self.records[run_id]
            if record["state"] in TERMINAL:
                return json.loads(json.dumps(record, allow_nan=False))
            if run_id != self.active_run or self.cancelled is None:
                raise RuntimeError("task is not owned by this service process")
            cancelled = self.cancelled
            worker = self.worker
            cancelled.set()
            record["state"] = "CANCELLING"
        self.robot.cancel_active()
        if worker is not None:
            worker.join(self.settings.model_timeout_s + self.settings.capture_timeout_s + 8.0)
            if worker.is_alive():
                raise RuntimeError("TASK_CANCEL_TIMEOUT: task did not stop within its deadline")
        return self.status(run_id)

    def close(self):
        with self.lock:
            self.closed = True
            run_id = self.active_run
        if run_id is not None:
            self.cancel(run_id)
