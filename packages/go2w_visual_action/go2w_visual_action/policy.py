"""Risk latch and motion-confirmation rules shared by the runner and its tests."""

SIGN_REASON = "prohibition_or_uncertain"
PERSON_REASON = "person_or_uncertain"
# One reading of a sign is noisy, so the robot resumes on the average of the last few rounds.
# Measured at 2.7 m: the "no entry" sign averages 0.50, the "welcome" sign 0.27.
RESUME_ROUNDS = 3
RESUME_PROBABILITY = 0.35
# No usable distance this round: skip the step and look again. A sign or a person can leave.
# A model-reported obstacle and a lidar block stay latched until the task is cancelled.
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
        return []
    recent = (recent + [score])[-RESUME_ROUNDS:]
    if len(recent) == RESUME_ROUNDS and sum(recent) / RESUME_ROUNDS < RESUME_PROBABILITY:
        return None
    return recent


def confirmed_forward(result):
    """True only when the chassis observed the commanded motion and acknowledged StopMove.

    ``not_observed`` means the burst was dispatched and stopped without qualified
    velocity feedback. Stop acknowledgement is not evidence that the robot moved.
    """
    return (isinstance(result, dict)
            and result.get("status") == "completed"
            and result.get("stop_acknowledged") is True
            and result.get("physical_motion_observed") is True)
