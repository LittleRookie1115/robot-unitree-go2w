"""Go2-W gRPC/MCP primitives; an explicit user enable grants session authorization."""
import asyncio
import json
import math
import os
from pathlib import Path
import threading
from types import SimpleNamespace

from mcp.server.fastmcp.exceptions import ToolError
from robonix_api import Err, Ok, Primitive
import chassis_mcp
import chassis_pb2
import std_msgs_mcp
import std_msgs_pb2
import std_srvs_mcp
import std_srvs_pb2

from .controller import Limits, MotionController, load_limits
from .transport import NativeRuntimeTransport, RosTransport

ROOT = Path(__file__).resolve().parents[3]
provider = Primitive(id="go2w_chassis_motion", namespace="robonix/primitive/chassis")
controller = MotionController()
transport = None


@provider.on_init
def initialize(config):
    global controller, transport
    if not isinstance(config, dict):
        return Err("motion config must be an object")
    known = {"allow_motion", "operator_present", "safety_ack", "network_interface",
             "robot_address", "robot_variant", "state_time_basis", "allowed_modes", "limits"}
    if set(config) - known:
        return Err("unknown motion config field(s): " + ", ".join(sorted(set(config) - known)))
    if not isinstance(config.get("allow_motion", False), bool):
        return Err("allow_motion must be an explicit boolean")
    if config.get("robot_variant", "go2w") != "go2w":
        return Err("this native deployment targets the operator's Go2-W")
    if controller.status()["active"]:
        return Err("cannot reinitialize while a movement is active")
    try:
        limits = load_limits(fallback=Limits(**config.get("limits", {})))
        controller.close()
        if transport is not None:
            transport.close()
            transport = None
        if config.get("allow_motion", False):
            transport = RosTransport(ROOT, config, os.environ, limits=limits)
        factory = None
        if config.get("allow_motion", False):
            factory = lambda: RosTransport(
                ROOT, config, os.environ, limits=controller.limits)
        elif (config.get("robot_variant") == "go2w"
              and config.get("state_time_basis") == "dds-source-lowstate-tick-v1"):
            factory = lambda: NativeRuntimeTransport(
                ROOT, config, os.environ, limits=controller.limits)
        controller = MotionController(transport=transport, limits=limits,
                                      transport_factory=factory)
        return Ok()
    except Exception as error:
        return Err(f"native motion initialization rejected: {error}")


@provider.grpc("robonix/primitive/chassis/move")
def move(request, context):
    """Execute one bounded velocity burst within an existing human-enabled session."""
    result = controller.execute(request.command, is_active=context.is_active)
    return move_response(result)


def move_response(result):
    """Preserve the controller's evidence and result contract without inferring success."""
    return chassis_pb2.ExecuteMoveCommand_Response(
        status=std_msgs_pb2.String(data=json.dumps(result)),
    )


def direction_response(request, context, axis, sign):
    """Convert positive magnitudes to a single canonical, controller-limited command."""
    names = ("linear_x", "linear_y", "linear_z", "angular_x", "angular_y", "angular_z",
             "duration_sec", "forward_m", "rotate_deg")
    try:
        values = {name: float(getattr(request.command, name)) for name in names}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("all command fields must be finite")
        if values["duration_sec"] < 0:
            raise ValueError("duration_sec must not be negative")
        supported = {axis, "duration_sec"}
        if axis == "linear_x":
            supported.add("forward_m")
        unsupported = [name for name in names if name not in supported and values[name] != 0]
        if unsupported:
            raise ValueError("unsupported field(s) for this direction: " + ", ".join(unsupported))
        if values[axis] < 0 or values["forward_m"] < 0:
            raise ValueError("direction RPCs require positive magnitudes; the RPC selects the sign")
        if values["forward_m"]:
            if values[axis] or values["duration_sec"]:
                raise ValueError("forward_m cannot be combined with velocity or duration_sec")
            command = chassis_pb2.MoveCommand(forward_m=sign * values["forward_m"])
        else:
            limits = controller.limits
            default_speed = (limits.lateral_mps if axis == "linear_y" else
                             limits.forward_mps if sign > 0 else limits.reverse_mps)
            command = chassis_pb2.MoveCommand(**{
                axis: sign * (values[axis] or default_speed),
                "duration_sec": values["duration_sec"] or limits.default_duration_s,
            })
    except (ValueError, TypeError, OverflowError) as error:
        return move_response({"status": "rejected", "error": str(error)})
    return move_response(controller.execute(command, is_active=context.is_active))


@provider.grpc("robonix/primitive/chassis/forward")
def forward(request, context):
    """Forward: positive linear_x and duration_sec, or positive forward_m."""
    return direction_response(request, context, "linear_x", 1)


@provider.grpc("robonix/primitive/chassis/backward")
def backward(request, context):
    """Backward: positive linear_x and duration_sec, or positive forward_m."""
    return direction_response(request, context, "linear_x", -1)


@provider.grpc("robonix/primitive/chassis/left")
def left(request, context):
    """Left: positive linear_y and duration_sec; distance shortcuts are unsupported."""
    return direction_response(request, context, "linear_y", 1)


@provider.grpc("robonix/primitive/chassis/right")
def right(request, context):
    """Right: positive linear_y and duration_sec; distance shortcuts are unsupported."""
    return direction_response(request, context, "linear_y", -1)


@provider.grpc("robonix/primitive/chassis/arm")
def arm(request, context):
    """Compatibility alias for operator enable."""
    result = controller.arm(is_active=context.is_active)
    return std_srvs_pb2.Trigger_Response(
        success=result.get("status") == "armed", message=json.dumps(result))

@provider.grpc("robonix/primitive/chassis/enable")
def enable(request, context):
    """Enable chassis motion only when the user explicitly requests enable.

    A Client message such as '使能底盘' or '启用底盘运动' authorizes this call.
    Never infer enable permission from a movement/posture request, startup, or
    a previous failure; recovery needs a new explicit user enable request.
    This performs zero-speed preparation only, without posture or nonzero motion.
    """
    result = controller.enable(is_active=context.is_active)
    return std_srvs_pb2.Trigger_Response(
        success=enable_succeeded(result), message=json.dumps(result))


def enable_succeeded(result):
    guard = result.get("guard")
    return (result.get("status") == "enabled" and result.get("session_enabled") is True
            and isinstance(guard, dict) and guard.get("ready") is True
            and guard.get("session_enabled") is True and guard.get("faulted") is not True
            and guard.get("guard_state") != "FAULT")


@provider.grpc("robonix/primitive/chassis/disable")
def disable(request):
    """Revoke session authorization through the controller's explicit disable path."""
    result = controller.disable()
    return std_srvs_pb2.Trigger_Response(
        success=result.get("status") == "disabled" and result.get("session_enabled") is False,
        message=json.dumps(result),
    )


@provider.grpc("robonix/primitive/chassis/stop")
def stop(request):
    """Cancel the current burst and stop, retaining healthy operator enable."""
    result = controller.stop(clear_fault=True)
    return chassis_pb2.Stop_Response(
        success=result["stop_acknowledged"], message=json.dumps(result),
    )


@provider.grpc("robonix/primitive/chassis/status")
@provider.grpc("robonix/primitive/chassis/motion_status")
def status(request):
    """Read current faulted, guard.ready and session_enabled; last_result is command history.

    If guard.can_arm is true and session_enabled is false, ask the user to
    explicitly request '使能底盘' in Client; use enable only after that request.
    A historical failed action alone does not mean the current backend is faulted.
    This status request never enables or moves the robot.
    """
    return std_srvs_pb2.Trigger_Response(
        success=True, message=json.dumps(controller.status()),
    )


def posture_response(action, context):
    result = controller.posture(action, is_active=context.is_active)
    return std_srvs_pb2.Trigger_Response(
        success=result.get("status") in ("dispatched", "completed") and
                result.get("action_finished") is True,
        message=json.dumps(result),
    )


@provider.grpc("robonix/primitive/chassis/stand_up")
def stand_up(request, context):
    """StandUp targets locked mode 6; request balance_stand explicitly for velocity mode 1."""
    return posture_response("stand_up", context)


@provider.grpc("robonix/primitive/chassis/stand_down")
def stand_down(request, context):
    """Explicit StandDown/crouch request; never called by lifecycle or health code."""
    return posture_response("stand_down", context)


@provider.grpc("robonix/primitive/chassis/lie_down")
def lie_down(request, context):
    """Compatibility name for explicit StandDown."""
    return posture_response("stand_down", context)


@provider.grpc("robonix/primitive/chassis/balance_stand")
def balance_stand(request, context):
    """Explicit BalanceStand targeting mode 1; never implicitly appended to StandUp."""
    return posture_response("balance_stand", context)


_mcp_jobs = set()


def _finish_mcp_job(job):
    _mcp_jobs.discard(job)
    if not job.cancelled():
        # Retrieve failures even when the caller was cancelled while the thread
        # continued its controller-owned cleanup.
        job.exception()


async def _mcp_invoke(handler, request, owns_context):
    active = threading.Event() if owns_context else None
    if active is not None:
        active.set()
        arguments = (request, SimpleNamespace(is_active=active.is_set))
    else:
        arguments = (request,)
    job = asyncio.create_task(asyncio.to_thread(handler, *arguments))
    _mcp_jobs.add(job)
    job.add_done_callback(_finish_mcp_job)
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        if active is not None:
            active.clear()
        # The controller sees only this request's inactive context and decides
        # whether it owns cleanup. Never stop another request from this bridge.
        raise


def _mcp_movement_succeeded(leaf, request, result):
    if result.get("status") != "completed" or result.get("stop_acknowledged") is not True:
        return False
    command = request.command
    nonzero = leaf != "move" or any(
        getattr(command, name) != 0 for name in
        ("forward_m", "rotate_deg", "linear_x", "linear_y", "angular_z")
    )
    return (result.get("dispatch_verified") is nonzero and
            result.get("physical_motion_observed") is nonzero)


def _mcp_response(leaf, request, response):
    if leaf in ("move", "forward", "backward", "left", "right"):
        payload = response.status.data
        result = json.loads(payload)
        if not _mcp_movement_succeeded(leaf, request, result):
            raise ToolError(payload)
        return chassis_mcp.ExecuteMoveCommand_Response(status=std_msgs_mcp.String(data=payload))
    payload = response.message
    result = json.loads(payload)
    if leaf in ("status", "motion_status"):
        success = response.success and result.get("status") not in (
            "not_observed", "not_dispatched", "rejected", "fault", "error",
            "cancelled", "stop_unconfirmed",
        )
    elif leaf == "enable":
        success = response.success and enable_succeeded(result)
    elif leaf == "stop":
        success = (response.success and result.get("status") == "stopped" and
                   result.get("stop_acknowledged") is True)
    elif leaf == "disable":
        success = (response.success and result.get("status") == "disabled" and
                   result.get("session_enabled") is False)
    else:
        success = (response.success and result.get("status") in ("dispatched", "completed") and
                   result.get("action_finished") is True)
    if not success:
        raise ToolError(payload)
    if leaf == "stop":
        return chassis_mcp.Stop_Response(success=response.success, message=payload)
    return std_srvs_mcp.Trigger_Response(success=response.success, message=payload)


def _register_mcp(leaf, handler, request_type, response_type, protobuf_type, owns_context):
    async def invoke(request):
        try:
            protobuf_request = protobuf_type(**request.to_dict())
            response = await _mcp_invoke(handler, protobuf_request, owns_context)
            return _mcp_response(leaf, protobuf_request, response)
        except ToolError:
            raise
        except (ValueError, TypeError, AttributeError) as error:
            raise ToolError(json.dumps({"status": "error", "error": str(error)})) from error

    invoke.__name__ = "mcp_" + leaf
    invoke.__doc__ = (handler.__doc__ or "").strip()
    if owns_context and leaf != "enable":
        invoke.__doc__ += " Requires prior human enable; this tool never enables or starts a backend."
    invoke.__annotations__ = {"request": request_type, "return": response_type}
    return provider.mcp("robonix/primitive/chassis/" + leaf)(invoke)


MCP_HANDLERS = {}
for _leaf, _handler in (
    ("move", move), ("forward", forward), ("backward", backward),
    ("left", left), ("right", right),
):
    MCP_HANDLERS[_leaf] = _register_mcp(
        _leaf, _handler, chassis_mcp.ExecuteMoveCommand_Request,
        chassis_mcp.ExecuteMoveCommand_Response, chassis_pb2.ExecuteMoveCommand_Request, True)
MCP_HANDLERS["stop"] = _register_mcp(
    "stop", stop, chassis_mcp.Stop_Request, chassis_mcp.Stop_Response, chassis_pb2.Stop_Request, False)
for _leaf, _handler, _owns_context in (
    ("enable", enable, True), ("disable", disable, False),
    ("status", status, False), ("motion_status", status, False),
    ("stand_up", stand_up, True), ("stand_down", stand_down, True),
    ("lie_down", lie_down, True), ("balance_stand", balance_stand, True),
):
    MCP_HANDLERS[_leaf] = _register_mcp(
        _leaf, _handler, std_srvs_mcp.Trigger_Request,
        std_srvs_mcp.Trigger_Response, std_srvs_pb2.Trigger_Request, _owns_context)


@provider.on_deactivate
def deactivate():
    controller.close()
    return Ok()


@provider.on_shutdown
def shutdown():
    global transport
    controller.close()
    if transport is not None:
        transport.close()
        transport = None
    return Ok()


if __name__ == "__main__":
    provider.run()
