#!/usr/bin/env python3
"""Verify disabled chassis discovery and model tools, without enabling or moving."""
import asyncio
import json
from pathlib import Path

import grpc
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import yaml

from chassis import motion_status
import atlas_pb2
import atlas_pb2_grpc


ROOT = Path(__file__).resolve().parent.parent
ATLAS = "127.0.0.1:50051"
PROVIDER = "go2_chassis_motion"


async def main():
    before = motion_status(ATLAS)
    assert not before["motion_enabled"] and before["backend_owner"] is None, before
    manifest = yaml.safe_load(
        (ROOT / "packages/go2_chassis_motion/package_manifest.yaml").read_text())
    ids = {entry["name"] for entry in manifest["capabilities"]}
    grpc_only = {"robonix/primitive/chassis/arm"}
    expected_tools = ids - grpc_only
    with grpc.insecure_channel(ATLAS) as channel:
        atlas = atlas_pb2_grpc.AtlasStub(channel)
        descriptors = atlas.ListContracts(atlas_pb2.ListContractsRequest(
            namespace_prefix="robonix/primitive/chassis"), timeout=5).contracts
        flags = {item.id: item.llm_callable if item.HasField("llm_callable") else True
                 for item in descriptors}
        for contract_id in ids:
            result = atlas.Query(atlas_pb2.QueryRequest(
                contract_id=contract_id, transport=atlas_pb2.TRANSPORT_GRPC), timeout=3)
            assert any(p.id == PROVIDER and p.state == atlas_pb2.STATE_ACTIVE
                       for p in result.providers), contract_id
            assert flags[contract_id] == (contract_id not in grpc_only), contract_id
        for contract_id in expected_tools:
            result = atlas.Query(atlas_pb2.QueryRequest(
                contract_id=contract_id, transport=atlas_pb2.TRANSPORT_MCP), timeout=3)
            assert any(p.id == PROVIDER and p.state == atlas_pb2.STATE_ACTIVE
                       for p in result.providers), contract_id
        binding = atlas.ConnectCapability(atlas_pb2.ConnectCapabilityRequest(
            consumer_id="chassis-catalog-readonly", provider_id=PROVIDER,
            contract_id="robonix/primitive/chassis/status",
            transport=atlas_pb2.TRANSPORT_MCP), timeout=3)
        try:
            async with streamablehttp_client(binding.endpoint, timeout=10) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert tools == {value.rsplit("/", 1)[-1] for value in expected_tools}, tools
                    status = await session.call_tool("status", {})
                    assert not status.isError, status
                    zero = await session.call_tool("move", {"command": {
                        "linear_x": 0.0, "linear_y": 0.0, "angular_z": 0.0,
                        "duration_sec": .01}})
                    assert zero.isError and any(
                        "motion_disabled" in getattr(content, "text", "")
                        for content in zero.content), "Disabled MCP move did not report its gate"
                    # Negative magnitudes are rejected before controller/SDK
                    # dispatch, even if a human enables concurrently.
                    invalid = await session.call_tool("forward", {
                        "command": {"linear_x": -0.01, "duration_sec": .01}})
                    assert invalid.isError, "Invalid movement was reported as a successful tool"
        finally:
            atlas.DisconnectCapability(
                atlas_pb2.DisconnectCapabilityRequest(channel_id=binding.channel_id), timeout=3)
    after = motion_status(ATLAS)
    assert not after["motion_enabled"] and after["backend_owner"] is None, after
    report = {
        "passed": True, "grpc_primitives": sorted(ids),
        "model_callable_primitives": sorted(expected_tools),
        "grpc_only_compatibility": sorted(grpc_only),
        "user_requested_enable": "robonix/primitive/chassis/enable",
        "enable_requires_explicit_user_request": True,
        "mcp_tools": sorted(tools), "invalid_mcp_movement_refused": True,
        "authorization_requests": [], "posture_requests": [],
        "nonzero_valid_movement_requests": [],
        "zero_velocity_request_refused_while_disabled": True,
        "status_after": after,
    }
    target = ROOT / "rbnx-build/native/chassis-catalog-acceptance.json"
    target.write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
