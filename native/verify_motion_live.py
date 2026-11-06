#!/usr/bin/env python3
"""Read-only acceptance after human enable; never creates or enables a backend."""
from datetime import datetime
import json
from pathlib import Path
import re
import urllib.request
from chassis import motion_status

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "127.0.0.1:50051"
MOTION_COUNTERS = ("move_attempt_count", "move_dispatch_count", "sdk_posture_dispatch_count")


def require_enabled():
    status = motion_status(ENDPOINT)
    assert status["motion_enabled"] is True, "A human must enable the session before this check"
    assert not status["active"] and not status["closed"] and not status["faulted"], status
    guard = status["guard"]
    assert guard["ready"] and guard["session_enabled"], guard
    assert guard["guard_state"] == "ARMED" and guard["daemon_armed"], guard
    assert guard["idle_timeout_sec"] is None, guard
    assert guard["state_time_basis"] == "dds-source-lowstate-tick-v1", guard
    assert guard["absolute_measurement_age_verified"] is False, guard
    return status


def dispatch_snapshot(status):
    dispatch = status["guard"]["dispatch"]
    counters = {}
    for name in MOTION_COUNTERS:
        value = dispatch.get(name)
        assert type(value) is int and value >= 0, f"Missing or invalid dispatch counter: {name}"
        counters[name] = value
    instance = dispatch.get("motion_evidence_instance")
    assert isinstance(instance, str) and instance, "Missing motion evidence instance"
    return {"backend_owner": status.get("backend_owner"),
            "motion_evidence_instance": instance, "counters": counters}


def capture_session(status):
    directory = ROOT / "rbnx-build/native" / (
        "primitive-chassis" if status.get("backend_owner") == "primitive" else "live")
    metadata = json.loads((directory / "session.json").read_text())
    assert metadata["armed_at_start"] is False, metadata
    assert metadata["postures_at_start"] == [], metadata
    offset = metadata["log_start_bytes"]["sdk"]
    assert type(offset) is int and offset >= 0, "Invalid SDK session log offset"
    sdk_path = directory / "sdk.log"
    check_start = sdk_path.stat().st_size
    assert check_start >= offset, "SDK log was truncated since this session started"
    return {"directory": directory, "metadata": metadata, "sdk_path": sdk_path,
            "session_log_start_bytes": offset, "check_start_bytes": check_start}


def assert_no_dispatch(baseline, status):
    current = dispatch_snapshot(status)
    assert current == baseline, {
        "error": "Motion dispatch counters or backend instance changed during the check",
        "before": baseline, "after": current,
    }
    return current


def verify_dispatch_interval(baseline, status, session):
    current = assert_no_dispatch(baseline, status)
    latest = capture_session(status)
    assert latest["directory"] == session["directory"], "Backend log directory changed"
    assert latest["metadata"] == session["metadata"], "Backend session restarted during the check"
    assert latest["check_start_bytes"] >= session["check_start_bytes"], "SDK log was truncated"
    with session["sdk_path"].open("rb") as stream:
        stream.seek(session["check_start_bytes"])
        log = stream.read().decode(errors="replace")
        check_end = stream.tell()
    # Counters are authoritative; cover verified replies and legacy logs while
    # asynchronous diagnostics may still be in transit to the worker.
    move_logged = bool(re.search(r"\bapi_id=1008\b", log) or
                       "Verified native Move acceptance" in log or "one-way Move emission" in log)
    posture_logged = bool(re.search(r"\bapi_id=(?:1002|1004|1005)\b", log))
    assert not move_logged, "A Move attempt appeared in the SDK log during the zero-only check"
    assert not posture_logged, "A posture request appeared in the SDK log during the check"
    return {
        "before": baseline, "after": current,
        "counter_deltas": {name: 0 for name in MOTION_COUNTERS},
        "runtime_directory": str(session["directory"]),
        "session": session["metadata"],
        "sdk_log_session_start_bytes": session["session_log_start_bytes"],
        "sdk_log_check_start_bytes": session["check_start_bytes"],
        "sdk_log_check_end_bytes": check_end,
        "move_requests_in_check_log": move_logged,
        "posture_requests_in_check_log": posture_logged,
    }


def main():
    status = require_enabled()
    baseline = dispatch_snapshot(status)
    session = capture_session(status)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:18092/status", timeout=3) as response:
        sensors = json.load(response)
    assert sensors["robot_variant"] == "go2w", sensors
    assert sensors["motion_enabled_scope"] == "sensor_observer_only"
    with opener.open("http://127.0.0.1:7860/api/system", timeout=5) as response:
        system = json.load(response)
    providers = {item["id"]: item["state"] for item in system["providers"]}
    assert providers.get("go2_chassis_motion") == "ACTIVE", providers
    final_status = require_enabled()
    evidence = verify_dispatch_interval(baseline, final_status, session)
    report = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "robot_variant": "go2w", "status": status, "status_after": final_status,
        "providers": providers,
        "verification_operations": ["motion_status", "sensor_status", "provider_status"],
        "dispatch_evidence": evidence,
        "absolute_measurement_age_verified": False,
        "physical_movement_or_posture_acceptance": False,
        "passed": True,
    }
    path = ROOT / "rbnx-build/native/motion-live-enabled-acceptance.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
