#!/usr/bin/env python3
"""Check default RPC wiring with invalid movement; never invoke authorization or posture."""
from datetime import datetime
import json
from pathlib import Path
import subprocess

from chassis import connected, motion_status
import atlas_pb2
import atlas_pb2_grpc
import chassis_pb2
import grpc
import robonix_contracts_pb2_grpc as contracts

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "127.0.0.1:50051"
STUB_NAMES = {
    "enable": "RobonixPrimitiveChassisEnableStub",
    "arm": "RobonixPrimitiveChassisArmStub",
    "disable": "RobonixPrimitiveChassisDisableStub",
    "move": "RobonixPrimitiveChassisMoveStub",
    "forward": "RobonixPrimitiveChassisForwardStub",
    "backward": "RobonixPrimitiveChassisBackwardStub",
    "left": "RobonixPrimitiveChassisLeftStub",
    "right": "RobonixPrimitiveChassisRightStub",
    "stop": "RobonixPrimitiveChassisStopStub",
    "motion_status": "RobonixPrimitiveChassisMotionStatusStub",
    "status": "RobonixPrimitiveChassisStatusStub",
    "stand_up": "RobonixPrimitiveChassisStandUpStub",
    "stand_down": "RobonixPrimitiveChassisStandDownStub",
    "lie_down": "RobonixPrimitiveChassisLieDownStub",
    "balance_stand": "RobonixPrimitiveChassisBalanceStandStub",
}


def require_disabled():
    status = motion_status(ENDPOINT)
    assert status["motion_enabled"] is False, "Refusing this check with a motion backend"
    assert status["backend_owner"] is None, "Refusing this check while a backend has an owner"
    assert not status.get("active") and not status.get("session_enabled"), status
    return status


def query_registration(action):
    """Check discovery and generated stub existence without invoking the primitive."""
    stub_name = STUB_NAMES[action]
    assert callable(getattr(contracts, stub_name, None)), f"Missing generated gRPC stub: {stub_name}"
    contract_id = "robonix/primitive/chassis/" + action
    with grpc.insecure_channel(ENDPOINT) as channel:
        result = atlas_pb2_grpc.AtlasStub(channel).Query(atlas_pb2.QueryRequest(
            contract_id=contract_id, transport=atlas_pb2.TRANSPORT_GRPC), timeout=3)
    providers = [item for item in result.providers
                 if item.id == "go2_chassis_motion" and item.state == atlas_pb2.STATE_ACTIVE]
    assert len(providers) == 1, f"Expected one active provider for {contract_id}"
    return {"contract_id": contract_id, "provider_id": providers[0].id,
            "generated_stub": stub_name, "verification": "atlas_query_and_stub_only"}


def main():
    status = require_disabled()
    registrations = {action: query_registration(action) for action in STUB_NAMES}
    moves = {}
    for action, stub_type in (
        ("move", contracts.RobonixPrimitiveChassisMoveStub),
        ("forward", contracts.RobonixPrimitiveChassisForwardStub),
        ("backward", contracts.RobonixPrimitiveChassisBackwardStub),
        ("left", contracts.RobonixPrimitiveChassisLeftStub),
        ("right", contracts.RobonixPrimitiveChassisRightStub),
    ):
        require_disabled()
        # These remain invalid even if a human enables between the status check
        # and this RPC. Never use a valid movement to test a disabled deployment.
        command = (chassis_pb2.MoveCommand(linear_x=1000.0, duration_sec=.01)
                   if action == "move" else
                   chassis_pb2.MoveCommand(linear_y=-.01, duration_sec=.01)
                   if action in ("left", "right") else
                   chassis_pb2.MoveCommand(linear_x=-.01, duration_sec=.01))
        with connected(ENDPOINT, "robonix/primitive/chassis/" + action) as channel:
            response = stub_type(channel).ExecuteMoveCommand(
                chassis_pb2.ExecuteMoveCommand_Request(command=command), timeout=3)
        result = json.loads(response.status.data)
        assert result.get("status") == "rejected" and result.get("error"), result
        moves[action] = result
    require_disabled()
    with connected(ENDPOINT, "robonix/primitive/chassis/stop") as channel:
        response = contracts.RobonixPrimitiveChassisStopStub(channel).Stop(
            chassis_pb2.Stop_Request(), timeout=3)
    assert response.success is False
    stopped = json.loads(response.message)
    assert stopped["status"] == "disabled" and stopped["stop_acknowledged"] is False, stopped
    final_status = require_disabled()
    processes = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                               text=True, check=True).stdout.splitlines()
    for line in processes:
        if "ps -eo" in line or "verify_motion.py" in line:
            continue
        assert "/bin/go2_sport_daemon " not in line, "A Sport daemon is unexpectedly running"
        assert "/go2_chassis_motion/ros_worker.py " not in line, "A motion ROS worker is running"
    report = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "motion_enabled": False, "backend_owner": None,
        "status_rpc": status, "status_after": final_status,
        "registered_primitives": registrations,
        "authorization_rpcs_invoked": [],
        "posture_rpcs_invoked": [],
        "invalid_move_rpc_rejected": moves["move"],
        "invalid_direction_rpcs_rejected": {
            action: result for action, result in moves.items() if action != "move"},
        "stop_did_not_claim_hardware_acknowledgement": stopped,
        "posture_rpcs_checked_by_registration_only": {
            action: registrations[action]
            for action in ("stand_up", "stand_down", "lie_down", "balance_stand")},
        "motion_backend_processes_absent": True,
        "passed": True,
    }
    path = ROOT / "rbnx-build/native/motion-disabled-acceptance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
