#!/usr/bin/env python3
"""Explicit Go2-W commands. Human 'enable' authorizes a persistent guarded session."""
import argparse
import json
import math
from pathlib import Path
import sys
import contextlib

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "packages/go2w_chassis_motion/rbnx-build/codegen/proto_gen"))
import grpc
import atlas_pb2
import atlas_pb2_grpc
import chassis_pb2
import robonix_contracts_pb2_grpc as contracts
import std_srvs_pb2


@contextlib.contextmanager
def connected(atlas_endpoint, contract_id):
    with grpc.insecure_channel(atlas_endpoint) as channel:
        atlas = atlas_pb2_grpc.AtlasStub(channel)
        result = atlas.Query(atlas_pb2.QueryRequest(
            contract_id=contract_id, transport=atlas_pb2.TRANSPORT_GRPC), timeout=3)
        providers = [p for p in result.providers if p.id == "go2w_chassis_motion"
                     and p.state == atlas_pb2.STATE_ACTIVE]
        if len(providers) != 1:
            raise RuntimeError("An active go2w_chassis_motion provider was not found")
        binding = atlas.ConnectCapability(atlas_pb2.ConnectCapabilityRequest(
            consumer_id="go2-local-chassis-cli", provider_id=providers[0].id,
            contract_id=contract_id, transport=atlas_pb2.TRANSPORT_GRPC), timeout=3)
        try:
            with grpc.insecure_channel(binding.endpoint) as provider_channel:
                yield provider_channel
        finally:
            atlas.DisconnectCapability(
                atlas_pb2.DisconnectCapabilityRequest(channel_id=binding.channel_id), timeout=3)


def motion_status(endpoint):
    with connected(endpoint, "robonix/primitive/chassis/motion_status") as channel:
        result = contracts.RobonixPrimitiveChassisMotionStatusStub(channel).Trigger(
            std_srvs_pb2.Trigger_Request(), timeout=5)
        return json.loads(result.message)


def finite_number(value):
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError("must be a finite number") from error
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be a finite number")
    return number


def positive_number(value):
    number = finite_number(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def nonnegative_number(value):
    number = finite_number(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def command_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas", default="127.0.0.1:50051")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    commands.add_parser("arm")
    commands.add_parser("enable")
    commands.add_parser("disable")
    commands.add_parser("stop")
    commands.add_parser("stand", aliases=["stand_up"])
    commands.add_parser("crouch", aliases=["lie_down", "stand_down"])
    commands.add_parser("balance", aliases=["balance_stand"])
    velocity = commands.add_parser("velocity", aliases=["move"])
    velocity.add_argument("--vx", type=finite_number, default=0.0, help="signed X velocity, m/s")
    velocity.add_argument("--vy", type=finite_number, default=0.0, help="signed Y velocity, m/s")
    velocity.add_argument("--wz", type=finite_number, default=0.0, help="signed yaw velocity, rad/s")
    velocity.add_argument("--duration", type=nonnegative_number, default=0.0,
                          help="seconds; zero/omitted uses the controller default")
    for direction in ("forward", "backward", "left", "right"):
        direction_parser = commands.add_parser(direction)
        magnitude = direction_parser.add_mutually_exclusive_group()
        magnitude.add_argument("--meters", type=positive_number,
                               help="positive distance, m; lateral compatibility uses 0.05 m/s")
        magnitude.add_argument("--speed", type=positive_number,
                               help="positive speed magnitude, m/s; RPC selects direction")
        direction_parser.add_argument("--duration", type=nonnegative_number, default=0.0,
                                      help="seconds; zero/omitted uses the controller default")
    turn = commands.add_parser("turn")
    turn.add_argument("--degrees", type=finite_number, required=True)
    return parser


TRIGGER_ACTIONS = {
    "enable": ("enable", "RobonixPrimitiveChassisEnableStub", 30),
    "arm": ("arm", "RobonixPrimitiveChassisArmStub", 30),
    "disable": ("disable", "RobonixPrimitiveChassisDisableStub", 15),
    "stand": ("stand_up", "RobonixPrimitiveChassisStandUpStub", 8),
    "stand_up": ("stand_up", "RobonixPrimitiveChassisStandUpStub", 8),
    "crouch": ("lie_down", "RobonixPrimitiveChassisLieDownStub", 8),
    "lie_down": ("lie_down", "RobonixPrimitiveChassisLieDownStub", 8),
    "stand_down": ("stand_down", "RobonixPrimitiveChassisStandDownStub", 8),
    "balance": ("balance_stand", "RobonixPrimitiveChassisBalanceStandStub", 8),
    "balance_stand": ("balance_stand", "RobonixPrimitiveChassisBalanceStandStub", 8),
}
DIRECTION_STUBS = {
    "forward": "RobonixPrimitiveChassisForwardStub",
    "backward": "RobonixPrimitiveChassisBackwardStub",
    "left": "RobonixPrimitiveChassisLeftStub",
    "right": "RobonixPrimitiveChassisRightStub",
}


def movement_command(args, parser):
    command = chassis_pb2.MoveCommand()
    if args.action in DIRECTION_STUBS:
        if args.meters is not None:
            if args.duration:
                parser.error("--meters cannot be combined with --duration")
            if args.action in ("forward", "backward"):
                # Direction primitives take positive magnitudes, including backward.
                command.forward_m = args.meters
            else:
                # Compatibility shortcut only: no lateral distance IDL or odometry is invented.
                command.linear_y = 0.05
                command.duration_sec = args.meters / command.linear_y
        else:
            axis = "linear_x" if args.action in ("forward", "backward") else "linear_y"
            setattr(command, axis, args.speed or 0.0)
            command.duration_sec = args.duration
        return args.action, DIRECTION_STUBS[args.action], command, True
    if args.action == "turn":
        command.rotate_deg = args.degrees
        expects_motion = bool(args.degrees)
    else:
        command.linear_x, command.linear_y, command.angular_z = args.vx, args.vy, args.wz
        command.duration_sec = args.duration
        expects_motion = bool(args.vx or args.vy or args.wz)
    return "move", "RobonixPrimitiveChassisMoveStub", command, expects_motion


def verify_movement_result(result, expects_motion):
    """A completed request needs confirmed stop and, when nonzero, both motion witnesses."""
    requirements = [
        ("status", result.get("status") == "completed", "'completed'"),
        ("stop_acknowledged", result.get("stop_acknowledged") is True, "true"),
    ]
    if expects_motion:
        requirements.extend(
            (field, result.get(field) is True, "true")
            for field in ("dispatch_verified", "physical_motion_observed")
        )
    return [
        f"missing {field}" if field not in result else f"{field} must be {expected}"
        for field, valid, expected in requirements if not valid
    ]


def main(argv=None):
    parser = command_parser()
    args = parser.parse_args(argv)
    verification_error = None
    try:
        if args.action == "status":
            result = motion_status(args.atlas)
            success = True
        elif args.action in TRIGGER_ACTIONS:
            action, stub_name, timeout = TRIGGER_ACTIONS[args.action]
            with connected(args.atlas, "robonix/primitive/chassis/" + action) as channel:
                response = getattr(contracts, stub_name)(channel).Trigger(
                    std_srvs_pb2.Trigger_Request(), timeout=timeout)
            result = json.loads(response.message)
            state = result.get("status")
            if action == "enable":
                success = response.success and state == "enabled"
            elif action == "arm":
                success = response.success and state in ("armed", "enabled")
            elif action == "disable":
                success = (response.success and state == "disabled" and
                           result.get("session_enabled") is False)
            else:
                success = (response.success and state in ("dispatched", "completed") and
                           result.get("action_finished") is True)
        elif args.action == "stop":
            with connected(args.atlas, "robonix/primitive/chassis/stop") as channel:
                response = contracts.RobonixPrimitiveChassisStopStub(channel).Stop(
                    chassis_pb2.Stop_Request(), timeout=5)
            result = json.loads(response.message)
            success = response.success and result.get("status") == "stopped"
        else:
            action, stub_name, command, expects_motion = movement_command(args, parser)
            with connected(args.atlas, "robonix/primitive/chassis/" + action) as channel:
                response = getattr(contracts, stub_name)(channel).ExecuteMoveCommand(
                    chassis_pb2.ExecuteMoveCommand_Request(command=command), timeout=5)
            result = json.loads(response.status.data)
            failures = verify_movement_result(result, expects_motion)
            success = not failures
            if failures and result.get("status") in ("completed", "dispatched"):
                verification_error = "; ".join(failures)
    except grpc.RpcError as error:
        result = {"status": "error", "error": error.details(),
                  "grpc_code": error.code().name}
        success = False
    except (RuntimeError, ValueError, TypeError, AttributeError) as error:
        result = {"status": "error", "error": str(error)}
        success = False
    print(json.dumps(result, indent=2))
    if verification_error is not None:
        # Preserve the server result on stdout while explaining rejected success claims.
        print(json.dumps({"verification_error": verification_error}), file=sys.stderr)
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
