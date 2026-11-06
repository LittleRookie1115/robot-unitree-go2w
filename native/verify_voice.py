#!/usr/bin/env python3
"""Verify real speech and reverse PCM transport without submitting a Pilot task."""
import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

from google.protobuf.empty_pb2 import Empty
import grpc
import websockets

from chassis import motion_status
import asr_pb2
import atlas_pb2
import atlas_pb2_grpc
import audio_pb2
import robonix_contracts_pb2_grpc as contracts
import tts_pb2

ROOT = Path(__file__).resolve().parent.parent
ATLAS = "127.0.0.1:50051"
PHRASE = "请告诉我当前的电池电量。"
CHANNEL_OPTIONS = (("grpc.enable_http_proxy", 0),)


@contextmanager
def connection(provider, contract):
    with grpc.insecure_channel(ATLAS, options=CHANNEL_OPTIONS) as channel:
        atlas = atlas_pb2_grpc.AtlasStub(channel)
        binding = atlas.ConnectCapability(atlas_pb2.ConnectCapabilityRequest(
            consumer_id="native-voice-acceptance", provider_id=provider,
            contract_id=contract, transport=atlas_pb2.TRANSPORT_GRPC), timeout=5)
        try:
            with grpc.insecure_channel(binding.endpoint, options=CHANNEL_OPTIONS) as target:
                yield target
        finally:
            atlas.DisconnectCapability(atlas_pb2.DisconnectCapabilityRequest(
                channel_id=binding.channel_id), timeout=3)


def synthesize():
    with connection("speech", "robonix/service/speech/tts") as channel:
        response = contracts.RobonixServiceSpeechTtsStub(channel).Synthesize(
            tts_pb2.Synthesize_Request(text=PHRASE, language="zh-CN", speed=1.0),
            timeout=45)
    if response.error or response.encoding != "pcm_s16le" or response.sample_rate_hz != 16000:
        raise RuntimeError("Speech returned an error or incompatible audio format: " + response.error)
    pcm = bytes(response.audio_data)
    if len(pcm) < 3200 or len(pcm) % 2:
        raise RuntimeError("Speech returned insufficient or malformed PCM")
    return pcm


def bridge_info():
    with connection("audio_client_bridge", "robonix/primitive/audio/bridge_info") as channel:
        return contracts.RobonixPrimitiveAudioBridgeInfoStub(channel).GetAudioBridgeInfo(
            audio_pb2.GetAudioBridgeInfo_Request(), timeout=5)


def capture_and_recognize(counter):
    with connection("audio_client_bridge", "robonix/primitive/audio/mic") as mic_channel, \
            connection("speech", "robonix/service/speech/asr_stream") as asr_channel:
        mic = contracts.RobonixPrimitiveAudioMicStub(mic_channel).Mic(Empty(), timeout=45)
        try:
            def chunks():
                for chunk in mic:
                    counter["mic_bytes"] += len(chunk.data)
                    counter["mic_frames"] += 1
                    yield asr_pb2.AsrAudioChunk(chunk=chunk)

            events = list(contracts.RobonixServiceSpeechAsrStreamStub(
                asr_channel).RecognizeStream(chunks(), timeout=45))
        finally:
            mic.cancel()
    errors = [event.error for event in events if event.error]
    finals = [event.text for event in events if event.is_final and event.text]
    if errors or not finals:
        raise RuntimeError("ASR produced no final transcript: " + "; ".join(errors))
    return finals[-1]


def play(pcm):
    with connection("audio_client_bridge", "robonix/primitive/audio/speaker") as channel:
        chunks = (audio_pb2.AudioChunk(data=pcm[index:index+3200], sequence=index//3200,
                                      duration_s=len(pcm[index:index+3200])/32000)
                  for index in range(0, len(pcm), 3200))
        contracts.RobonixPrimitiveAudioSpeakerStub(channel).Speaker(chunks, timeout=30)


async def main():
    sys.path.insert(0, str(ROOT.parent / "robonix-client/src"))
    from robonix_client.audio_reverse_bridge import AudioReverseBridge

    before = motion_status(ATLAS)
    info = await asyncio.to_thread(bridge_info)
    if not info.reverse or info.connected:
        raise RuntimeError("Require an idle reverse bridge; an existing Client will not be replaced")
    pcm = await asyncio.to_thread(synthesize)
    received = bytearray()
    counter = {"mic_bytes": 0, "mic_frames": 0}
    speaker_done = asyncio.Event()

    async def synthetic_device(socket):
        if socket.request.path == "/mic":
            for index in range(0, len(pcm), 3200):
                await socket.send(pcm[index:index+3200])
        elif socket.request.path == "/speaker":
            try:
                async for message in socket:
                    if isinstance(message, bytes):
                        received.extend(message)
            finally:
                speaker_done.set()
        else:
            await socket.close(code=1008, reason="synthetic audio verification only")

    async with websockets.serve(synthetic_device, "127.0.0.1", 0, max_size=None) as devices:
        local_port = devices.sockets[0].getsockname()[1]
        relay = AudioReverseBridge(info.endpoint, local_port=local_port)
        relay.start()
        try:
            for _ in range(60):
                if (await asyncio.to_thread(bridge_info)).connected:
                    break
                await asyncio.sleep(.05)
            else:
                raise RuntimeError("The actual Client reverse bridge did not connect")
            transcript = await asyncio.wait_for(
                asyncio.to_thread(capture_and_recognize, counter), timeout=55)
            if "电池" not in transcript or not counter["mic_frames"]:
                raise RuntimeError("Unexpected synthetic ASR transcript: " + transcript)
            await asyncio.wait_for(asyncio.to_thread(play, pcm), timeout=35)
            await asyncio.wait_for(speaker_done.wait(), timeout=5)
            if bytes(received) != pcm or counter["mic_bytes"] != len(pcm):
                raise RuntimeError("Reverse audio transport changed or lost PCM bytes")
        finally:
            await asyncio.to_thread(relay.stop)
    after = motion_status(ATLAS)
    if before["session_enabled"] != after["session_enabled"]:
        raise RuntimeError("The motion session changed during audio-only verification")
    report = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "passed": True, "synthetic_phrase": PHRASE, "recognized_text": transcript,
        "audio_format": "16000 Hz mono pcm_s16le", "audio_bytes": len(pcm),
        "audio_seconds": len(pcm)/32000,
        "pcm_sha256": hashlib.sha256(pcm).hexdigest(), **counter,
        "reverse_speaker_bytes": len(received), "reverse_pcm_exact_match": True,
        "actual_client_reverse_bridge_tested": True,
        "synthetic_local_device_server": True,
        "pilot_tasks_submitted": [], "motion_authorization_requests": [],
        "posture_requests": [], "nonzero_motion_requests": [],
        "physical_laptop_microphone_tested": False,
        "physical_laptop_speaker_tested": False,
        "status_after": after,
    }
    target = ROOT / "rbnx-build/native/voice-acceptance.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
