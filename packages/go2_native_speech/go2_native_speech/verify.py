"""Real offline TTS -> ASR verification; never starts Atlas, playback or a Pilot task."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import sys
import time
from types import SimpleNamespace
import wave

import numpy as np

from .settings import ASR_CLASS, BUILD, SAMPLE_RATE, TTS_CLASS, MODEL_ROOT, default_speaker


def normalized(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text).lower()


def edit_distance(expected, actual):
    row = list(range(len(actual) + 1))
    for index, char in enumerate(expected, 1):
        next_row = [index]
        for offset, other in enumerate(actual, 1):
            next_row.append(min(next_row[-1] + 1, row[offset] + 1,
                                row[offset - 1] + (char != other)))
        row = next_row
    return row[-1]


class LocalContext:
    def __init__(self):
        self.code = None
        self.details = ""

    def is_active(self):
        return True

    def set_code(self, value):
        self.code = str(value)

    def set_details(self, value):
        self.details = value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="请告诉我当前电池电量")
    parser.add_argument("--voice", default=str(default_speaker()))
    args = parser.parse_args(argv)
    if not args.text.strip():
        parser.error("test text must not be empty")
    os.environ.update(
        SPEECH_BACKEND="custom", SPEECH_ASR_BACKEND_CLASS=ASR_CLASS,
        SPEECH_ASR_STREAM_BACKEND_CLASS=ASR_CLASS, SPEECH_TTS_BACKEND_CLASS=TTS_CLASS,
        SPEECH_TTS_OUTPUT_ENCODING="pcm_s16le", SPEECH_TTS_OUTPUT_SAMPLE_RATE="16000",
        TTS_VOICE=args.voice,
    )
    assert importlib.util.find_spec("torch") is None, "Use the isolated CPU Speech environment"
    import asr_pb2
    import tts_pb2
    from robonix_api import Ok
    from speech_service import service

    configuration = {
        "speech_backend": "custom", "speech_asr_backend_class": ASR_CLASS,
        "speech_asr_stream_backend_class": ASR_CLASS, "speech_tts_backend_class": TTS_CLASS,
        "wake_word_model_dir": str(MODEL_ROOT / "kws"),
        "wake_words": ["罗伯特"],
        "tts_voice": args.voice,
    }
    started = time.perf_counter()
    initialized = service.init(configuration)
    load_s = time.perf_counter() - started
    assert isinstance(initialized, Ok), initialized
    assert service._asr_servicer.asr_backend is not None
    assert service._asr_stream_servicer.stream_asr_backend is not None
    assert service._tts_servicer.tts_backend is not None
    assert (service._asr_servicer.asr_backend._engine is
            service._asr_stream_servicer.stream_asr_backend._engine)

    context = LocalContext()
    started = time.perf_counter()
    response = service._tts_servicer.Synthesize(
        tts_pb2.Synthesize_Request(text=args.text, language="zh-CN", voice=args.voice, speed=1.0), context)
    tts_s = time.perf_counter() - started
    assert not response.error and context.code is None, (response.error, context.details)
    assert response.encoding == "pcm_s16le" and response.sample_rate_hz == SAMPLE_RATE
    pcm = bytes(response.audio_data)
    assert pcm and len(pcm) % 2 == 0
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768
    assert np.max(np.abs(samples)) > .01, "TTS unexpectedly produced silence"
    duration = len(pcm) / (SAMPLE_RATE * 2)

    started = time.perf_counter()
    recognized = service._asr_servicer.Recognize(asr_pb2.Recognize_Request(
        audio_data=pcm, encoding="pcm_s16le", sample_rate_hz=SAMPLE_RATE, language="zh-CN"), context)
    asr_s = time.perf_counter() - started
    assert not recognized.error and context.code is None, (recognized.error, context.details)

    def chunks():
        for offset in range(0, len(pcm), 3200):
            chunk = pcm[offset:offset + 3200]
            yield SimpleNamespace(chunk=SimpleNamespace(
                data=chunk, duration_s=len(chunk) / (SAMPLE_RATE * 2)))

    started = time.perf_counter()
    events = list(service._asr_stream_servicer.RecognizeStream(chunks(), context))
    stream_s = time.perf_counter() - started
    finals = [event for event in events if event.is_final]
    assert len(finals) == 1 and not any(event.event_type == 2 for event in events)
    expected = normalized(args.text)
    one_text = normalized(recognized.text)
    stream_text = normalized(finals[0].text)
    distance = edit_distance(expected, one_text)
    stream_distance = edit_distance(expected, stream_text)
    passed = max(distance, stream_distance) / len(expected) <= .2
    BUILD.mkdir(parents=True, exist_ok=True)
    audio_path = BUILD / "tts-asr-roundtrip.wav"
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "real_inference": True, "network_rpc_used": False, "pilot_task_submitted": False,
        "audio_playback_performed": False, "provider": "cpu", "torch_installed": False,
        "configuration": configuration, "python": sys.version,
        "versions": {name: importlib.metadata.version(name) for name in (
            "sherpa-onnx", "numpy", "scipy", "robonix-api", "grpcio")},
        "text": args.text, "tts_speaker_id": args.voice,
        "asr_text": recognized.text, "asr_stream_final": finals[0].text,
        "asr_confidence_available": False, "asr_confidence": recognized.confidence,
        "character_error_rate": distance / len(expected),
        "stream_character_error_rate": stream_distance / len(expected),
        "format": {"encoding": "pcm_s16le", "sample_rate_hz": SAMPLE_RATE,
                   "channels": 1, "duration_s": duration, "bytes": len(pcm)},
        "timing": {"backend_init_and_warm_s": load_s, "tts_s": tts_s, "asr_s": asr_s,
                   "asr_stream_s": stream_s, "tts_rtf": tts_s / duration, "asr_rtf": asr_s / duration},
        "audio_rms": float(np.sqrt(np.mean(samples * samples))),
        "audio_peak": float(np.max(np.abs(samples))),
        "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "kws_backend_loaded": service._wake_word_servicer.backend is not None,
        "handsfree_or_wake_task_tested": False, "audio_path": str(audio_path),
        "model_provenance": {
            name: json.loads((MODEL_ROOT / directory / "provenance.json").read_text())
            for name, directory in (("asr", "sensevoice"), ("tts", "vits-zh-ll"))
        },
        "passed": passed,
    }
    (BUILD / "inference-evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit("Real inference completed but transcript accuracy needs attention")


if __name__ == "__main__":
    main()
