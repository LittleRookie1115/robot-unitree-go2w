"""Real CPU SenseVoice and VITS implementations of official Speech backend protocols."""
import asyncio
import math
import re
import threading

import numpy as np
from scipy.signal import resample_poly
import sherpa_onnx

from .settings import SAMPLE_RATE, asr_directory, default_speaker, threads, tts_directory

MAX_AUDIO_SECONDS = 120
MAX_TTS_CHARACTERS = 512
MAX_PCM_BYTES = 4 * 1024 * 1024 - 8192
_engines_lock = threading.Lock()
_asr_engines = {}
_tts_engines = {}


def _required(path):
    if not path.is_file():
        raise FileNotFoundError(f"Speech model file missing: {path}; build go2_native_speech first")
    return str(path)


def pcm_samples(audio_bytes, encoding="pcm_s16le", sample_rate=SAMPLE_RATE):
    if encoding != "pcm_s16le" or sample_rate != SAMPLE_RATE:
        raise ValueError("Native Speech backends require 16000 Hz mono pcm_s16le")
    if not isinstance(audio_bytes, (bytes, bytearray, memoryview)) or not audio_bytes:
        raise ValueError("PCM audio must contain at least one sample")
    if len(audio_bytes) % 2:
        raise ValueError("pcm_s16le audio must have an even byte length")
    if len(audio_bytes) > MAX_AUDIO_SECONDS * SAMPLE_RATE * 2:
        raise ValueError(f"ASR audio exceeds {MAX_AUDIO_SECONDS} seconds")
    return np.frombuffer(audio_bytes, dtype="<i2").astype(np.float32) / 32768.0


def pcm_bytes(samples, sample_rate):
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 1 or not samples.size or not np.isfinite(samples).all():
        raise ValueError("TTS produced empty, non-mono or non-finite audio")
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("TTS produced an invalid sample rate")
    if sample_rate != SAMPLE_RATE:
        divisor = math.gcd(sample_rate, SAMPLE_RATE)
        samples = resample_poly(samples, SAMPLE_RATE // divisor, sample_rate // divisor)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()


class SenseVoiceASRBackend:
    """One-shot ASR plus buffered final streaming; both instances share one CPU engine."""

    def __init__(self):
        directory = asr_directory().resolve()
        count = threads()
        key = (directory, count)
        with _engines_lock:
            if key not in _asr_engines:
                engine = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=_required(directory / "model.int8.onnx"),
                    tokens=_required(directory / "tokens.txt"),
                    num_threads=count, provider="cpu", language="auto",
                    use_itn=True, debug=False,
                )
                _asr_engines[key] = (engine, threading.Lock())
            self._engine, self._lock = _asr_engines[key]

    def recognize(self, audio_bytes, encoding="pcm_s16le", sample_rate=SAMPLE_RATE, language="zh-CN"):
        samples = pcm_samples(audio_bytes, encoding, sample_rate)
        with self._lock:
            stream = self._engine.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            self._engine.decode_stream(stream)
            text = re.sub(r"<\|[^>]*\|>", "", stream.result.text).strip()
        # SenseVoice exposes no calibrated probability through this API.
        return {"text": text, "confidence": 0.0, "confidence_available": False}

    def recognize_stream(self, pcm_chunks):
        buffer = bytearray()
        for chunk in pcm_chunks:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ValueError("ASR stream chunks must be PCM bytes")
            if len(chunk) % 2:
                raise ValueError("pcm_s16le stream chunks must have an even byte length")
            buffer.extend(chunk)
            if len(buffer) > MAX_AUDIO_SECONDS * SAMPLE_RATE * 2:
                raise ValueError(f"ASR stream exceeds {MAX_AUDIO_SECONDS} seconds")
        result = self.recognize(bytes(buffer))
        yield {"event_type": 1, "text": result["text"], "confidence": result["confidence"],
               "is_final": True}


class VitsTTSBackend:
    """Chinese VITS with output resampled to the official 16 kHz mono PCM contract."""

    def __init__(self):
        directory = tts_directory().resolve()
        count = threads()
        key = (directory, count)
        with _engines_lock:
            if key not in _tts_engines:
                config = sherpa_onnx.OfflineTtsConfig(
                    model=sherpa_onnx.OfflineTtsModelConfig(
                        vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                            model=_required(directory / "model.onnx"),
                            lexicon=_required(directory / "lexicon.txt"),
                            tokens=_required(directory / "tokens.txt"),
                            dict_dir=str(directory / "dict") if (directory / "dict").is_dir() else "",
                        ),
                        num_threads=count, provider="cpu", debug=False,
                    ),
                    rule_fsts=",".join(str(directory / name) for name in (
                        "phone.fst", "date.fst", "number.fst", "new_heteronym.fst"
                    ) if (directory / name).is_file()),
                    max_num_sentences=1,
                )
                if not config.validate():
                    raise ValueError("Invalid native VITS model configuration")
                _tts_engines[key] = (sherpa_onnx.OfflineTts(config), threading.Lock())
            self._engine, self._lock = _tts_engines[key]

    def _synthesize(self, text, voice, speed):
        from speech_service.tts_errors import TTSInputError
        from speech_service.tts_runtime import tts_request_is_active

        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TTS_CHARACTERS:
            raise TTSInputError(f"TTS text must contain 1..{MAX_TTS_CHARACTERS} characters")
        if isinstance(speed, bool) or not math.isfinite(speed) or not .5 <= speed <= 2:
            raise TTSInputError("TTS speed must be finite in [0.5, 2]")
        try:
            speaker = default_speaker() if voice in ("", "default", "zh-CN") else int(voice)
        except (ValueError, TypeError) as error:
            raise TTSInputError("VITS voice must be a numeric speaker id") from error
        if not 0 <= speaker < self._engine.num_speakers:
            raise TTSInputError("VITS speaker id is out of range")
        with self._lock:
            if not tts_request_is_active():
                raise RuntimeError("TTS request was cancelled")
            audio = self._engine.generate(text.strip(), sid=speaker, speed=speed)
            if not tts_request_is_active():
                raise RuntimeError("TTS request was cancelled")
            pcm = pcm_bytes(audio.samples, audio.sample_rate)
            if len(pcm) > MAX_PCM_BYTES:
                raise TTSInputError("TTS audio exceeds the one-shot IPC limit; split the text")
            return pcm

    async def synthesize(self, text, voice="", speed=1.0):
        return await asyncio.to_thread(self._synthesize, text, voice, speed)

    async def synthesize_stream(self, text, voice="", speed=1.0):
        from speech_service.tts_runtime import tts_request_is_active

        pcm = await self.synthesize(text, voice, speed)
        frame_bytes = SAMPLE_RATE * 2 // 10
        for offset in range(0, len(pcm), frame_bytes):
            if not tts_request_is_active():
                return
            yield pcm[offset:offset + frame_bytes]
            await asyncio.sleep(0)
