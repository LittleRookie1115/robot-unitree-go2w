#!/usr/bin/env python3
"""Print loopback ports for a laptop SSH tunnel; never submit a robot task."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages/go2w_chassis_motion/rbnx-build/codegen/proto_gen"))

import grpc
import atlas_pb2
import atlas_pb2_grpc
import audio_pb2
import robonix_contracts_pb2_grpc as contracts


BRIDGE = "audio_client_bridge"
SPEECH = "speech"
BRIDGE_INFO = "robonix/primitive/audio/bridge_info"
CONSUMER = "robonix-laptop-ssh-discovery"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def endpoint_port(endpoint):
    parsed = urlsplit(endpoint if "://" in endpoint else "http://" + endpoint)
    if parsed.hostname not in LOOPBACK or parsed.username or parsed.password:
        raise ValueError("SSH discovery expects a loopback endpoint")
    if parsed.scheme not in ("http", "https", "grpc", "ws", "wss"):
        raise ValueError("Unsupported endpoint scheme")
    port = parsed.port
    if port is None or not 1 <= port <= 65535:
        raise ValueError("Endpoint has no valid TCP port")
    return port


def discover(atlas, atlas_endpoint, bridge_query, include_preview=False):
    providers = atlas.Query(atlas_pb2.QueryRequest(
        transport=atlas_pb2.TRANSPORT_GRPC), timeout=5).providers
    active = {item.id: item for item in providers if item.state == atlas_pb2.STATE_ACTIVE}
    if not {BRIDGE, SPEECH} <= set(active):
        raise RuntimeError("Start the native audio_client_bridge and speech providers first")
    speech_contracts = {item.contract_id for item in active[SPEECH].capabilities}
    if not {"robonix/service/speech/asr_stream", "robonix/service/speech/tts"} <= speech_contracts:
        raise RuntimeError("Speech ASR streaming and TTS capabilities are required")
    atlas_port = endpoint_port(atlas_endpoint)
    ports = {atlas_port}
    reverse_port = None
    for provider in active.values():
        for capability in provider.capabilities:
            if capability.transport != atlas_pb2.TRANSPORT_GRPC:
                continue
            binding = atlas.ConnectCapability(atlas_pb2.ConnectCapabilityRequest(
                consumer_id=CONSUMER, provider_id=provider.id,
                contract_id=capability.contract_id, transport=atlas_pb2.TRANSPORT_GRPC),
                timeout=3)
            try:
                ports.add(endpoint_port(binding.endpoint))
                if provider.id == BRIDGE and capability.contract_id == BRIDGE_INFO:
                    info = bridge_query(binding.endpoint)
                    if not info.reverse:
                        raise RuntimeError("The client audio bridge must use reverse transport")
                    reverse_port = endpoint_port(info.endpoint)
                    ports.add(reverse_port)
            finally:
                atlas.DisconnectCapability(atlas_pb2.DisconnectCapabilityRequest(
                    channel_id=binding.channel_id), timeout=3)
    if reverse_port is None:
        raise RuntimeError("The active audio bridge did not declare bridge_info")
    if include_preview or "go2w_native" in active:
        ports.add(18092)
    return {
        "atlas_port": atlas_port, "ports": sorted(ports),
        "audio_bridge_provider": BRIDGE, "speech_provider": SPEECH,
        "audio_reverse_port": reverse_port,
    }


def bridge_query(endpoint):
    with grpc.insecure_channel(endpoint) as channel:
        return contracts.RobonixPrimitiveAudioBridgeInfoStub(channel).GetAudioBridgeInfo(
            audio_pb2.GetAudioBridgeInfo_Request(), timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas", default="127.0.0.1:50051")
    parser.add_argument("--include-preview", action="store_true")
    args = parser.parse_args()
    try:
        with grpc.insecure_channel(args.atlas) as channel:
            result = discover(atlas_pb2_grpc.AtlasStub(channel), args.atlas,
                              bridge_query, args.include_preview)
        print(json.dumps(result))
        return 0
    except (grpc.RpcError, ValueError, RuntimeError) as error:
        print("Voice tunnel discovery failed: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
