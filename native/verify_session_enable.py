#!/usr/bin/env python3
"""Verify human enable persists through zero-speed requests, stop and idle.

Requires an already enabled session. Never enables it, dispatches nonzero
velocity, or calls a posture API.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import time

from chassis import connected
import chassis_pb2
import robonix_contracts_pb2_grpc as contracts
from verify_motion_live import (
    ENDPOINT, assert_no_dispatch, capture_session, dispatch_snapshot,
    require_enabled, verify_dispatch_interval,
)

ROOT = Path(__file__).resolve().parent.parent


def zero_request():
    with connected(ENDPOINT, "robonix/primitive/chassis/move") as channel:
        response = contracts.RobonixPrimitiveChassisMoveStub(channel).ExecuteMoveCommand(
            chassis_pb2.ExecuteMoveCommand_Request(
                command=chassis_pb2.MoveCommand(duration_sec=0.1)), timeout=5)
    result = json.loads(response.status.data)
    assert result["status"] == "completed" and result["stop_acknowledged"] is True, result
    assert result.get("dispatch_verified") is False, result
    assert result.get("physical_motion_observed") is False, result
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idle-seconds", type=float, default=120.0)
    args = parser.parse_args(argv)
    if not 16 <= args.idle_seconds <= 600:
        parser.error("--idle-seconds must be between 16 and 600")
    status = require_enabled()
    baseline = dispatch_snapshot(status)
    session = capture_session(status)

    def checked_status():
        current = require_enabled()
        assert_no_dispatch(baseline, current)
        return current

    report = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "status_before": status,
        "nonzero_velocity_requests": [],
        "posture_requests": [],
    }
    report["first_zero_request"] = zero_request()
    report["after_first_request"] = checked_status()
    with connected(ENDPOINT, "robonix/primitive/chassis/stop") as channel:
        response = contracts.RobonixPrimitiveChassisStopStub(channel).Stop(
            chassis_pb2.Stop_Request(), timeout=5)
    report["explicit_stop"] = json.loads(response.message)
    assert response.success and report["explicit_stop"]["stop_acknowledged"] is True, report
    report["after_explicit_stop"] = checked_status()
    started = time.monotonic()
    while time.monotonic() - started < args.idle_seconds:
        report["after_idle"] = checked_status()
        time.sleep(0.2)
    report["idle_seconds"] = time.monotonic() - started
    report["second_zero_request"] = zero_request()
    report["status_after"] = checked_status()
    report["dispatch_evidence"] = verify_dispatch_interval(baseline, report["status_after"], session)
    report["passed"] = True
    report["physical_movement_or_posture_acceptance"] = False
    path = ROOT / "rbnx-build/native/session-enable-acceptance.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
