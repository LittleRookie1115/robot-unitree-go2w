import asyncio
import json
import os

from robonix_api import Ok, Service

import go2w_visual_action_pb2
from go2w_visual_action_mcp import (
    Cancel_Request,
    Cancel_Response,
    Evaluate_Request,
    Evaluate_Response,
    RunStep_Request,
    RunStep_Response,
    Status_Request,
    Status_Response,
)

from .robot import Robot
from .runtime import Runtime
from .settings import Settings


CONTRACT_ROOT = "robonix/service/go2w_visual_action"
provider = Service(
    id=os.environ.get("RBNX_INSTANCE_NAME", "go2w_visual_action"),
    namespace=CONTRACT_ROOT,
)
runtime = None


def service():
    if runtime is None:
        raise RuntimeError("Go2-W visual action service is not initialized")
    return runtime


def evaluation(request):
    result = service().evaluate(request)
    return {
        "decision_id": result["decision_id"],
        "forward_allowed": result["forward_allowed"],
        "person_probability": result["person_probability"],
        "prohibition_probability": result["prohibition_probability"],
        "obstacle_probability": result["obstacle_probability"],
        "camera_age_s": result["camera_age_s"],
        "reason": result["reason"],
        "result_json": json.dumps(result, ensure_ascii=False, allow_nan=False),
    }


def task_status(record):
    return {
        "run_id": record["run_id"],
        "state": record["state"],
        "error_code": record["error_code"],
        "error_message": record["error_message"],
        "motion_requested": record["motion_requested"],
        "stop_requested": record["stop_requested"],
        "stop_confirmed": record["stop_confirmed"],
        "result_json": json.dumps(record, ensure_ascii=False, allow_nan=False),
    }


@provider.mcp(CONTRACT_ROOT + "/evaluate")
async def evaluate_mcp(request: Evaluate_Request) -> Evaluate_Response:
    result = await asyncio.to_thread(evaluation, request)
    return Evaluate_Response(**result)


@provider.grpc(CONTRACT_ROOT + "/evaluate")
def evaluate_grpc(request):
    return go2w_visual_action_pb2.Evaluate_Response(**evaluation(request))


@provider.mcp(CONTRACT_ROOT + "/run_step")
def run_step_mcp(request: RunStep_Request) -> RunStep_Response:
    record = service().start(request)
    return RunStep_Response(run_id=record["run_id"], state=record["state"])


@provider.grpc(CONTRACT_ROOT + "/run_step")
def run_step_grpc(request):
    record = service().start(request)
    return go2w_visual_action_pb2.RunStep_Response(
        run_id=record["run_id"], state=record["state"],
    )


@provider.mcp(CONTRACT_ROOT + "/status")
def status_mcp(request: Status_Request) -> Status_Response:
    return Status_Response(**task_status(service().status(request.run_id)))


@provider.grpc(CONTRACT_ROOT + "/status")
def status_grpc(request):
    return go2w_visual_action_pb2.Status_Response(
        **task_status(service().status(request.run_id)),
    )


@provider.mcp(CONTRACT_ROOT + "/cancel")
def cancel_mcp(request: Cancel_Request) -> Cancel_Response:
    record = service().cancel(request.run_id)
    return Cancel_Response(
        run_id=record["run_id"],
        state=record["state"],
        stop_requested=record["stop_requested"],
        stop_confirmed=record["stop_confirmed"],
        result_json=json.dumps(record, ensure_ascii=False, allow_nan=False),
    )


@provider.grpc(CONTRACT_ROOT + "/cancel")
def cancel_grpc(request):
    record = service().cancel(request.run_id)
    return go2w_visual_action_pb2.Cancel_Response(
        run_id=record["run_id"],
        state=record["state"],
        stop_requested=record["stop_requested"],
        stop_confirmed=record["stop_confirmed"],
        result_json=json.dumps(record, ensure_ascii=False, allow_nan=False),
    )


@provider.on_init
def initialize(config):
    global runtime
    settings = Settings.model_validate(config)
    runtime = Runtime(settings, Robot(settings, provider.id))
    return Ok()


@provider.on_deactivate
def deactivate():
    if runtime is not None and runtime.active_run is not None:
        runtime.cancel(runtime.active_run)
    return Ok()


@provider.on_shutdown
def shutdown():
    if runtime is not None:
        runtime.close()
    return Ok()


if __name__ == "__main__":
    provider.run()
