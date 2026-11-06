"""Non-secret paths and bounded CPU settings; no credential environment is sourced."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BUILD = Path(os.environ.get("GO2_SPEECH_BUILD", str(ROOT / "rbnx-build/native/speech")))
MODEL_ROOT = BUILD / "models"
ASR_CLASS = "go2_native_speech.backends:SenseVoiceASRBackend"
TTS_CLASS = "go2_native_speech.backends:VitsTTSBackend"
SAMPLE_RATE = 16000


def default_speaker():
    return int(os.environ.get("GO2_SPEECH_TTS_SPEAKER", os.environ.get("TTS_VOICE", "4")))


def threads():
    value = int(os.environ.get("GO2_SPEECH_THREADS", "2"))
    if not 1 <= value <= 8:
        raise ValueError("GO2_SPEECH_THREADS must be in [1, 8]")
    return value


def asr_directory():
    return Path(os.environ.get("GO2_SPEECH_ASR_MODEL_DIR", str(MODEL_ROOT / "sensevoice")))


def tts_directory():
    return Path(os.environ.get("GO2_SPEECH_TTS_MODEL_DIR", str(MODEL_ROOT / "vits-zh-ll")))
