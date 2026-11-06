import asyncio
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

import numpy as np

from go2_native_speech import backends, models
from go2_native_speech.settings import SAMPLE_RATE
from speech_service.tts_errors import TTSInputError


class AudioContractTest(unittest.TestCase):
    def test_pcm_input_is_little_endian_and_normalized(self):
        pcm = np.array([-32768, 0, 32767], dtype="<i2").tobytes()
        samples = backends.pcm_samples(pcm)
        self.assertEqual(samples.dtype, np.float32)
        np.testing.assert_allclose(samples, [-1, 0, 32767 / 32768])

    def test_input_rejects_wrong_format_empty_odd_and_unbounded_audio(self):
        for pcm, encoding, rate in (
            (b"", "pcm_s16le", 16000), (b"\x01", "pcm_s16le", 16000),
            (b"\0\0", "wav", 16000), (b"\0\0", "pcm_s16le", 48000),
            (b"\0" * (backends.MAX_AUDIO_SECONDS * 32000 + 2), "pcm_s16le", 16000),
        ):
            with self.subTest(encoding=encoding, rate=rate, size=len(pcm)):
                with self.assertRaises(ValueError):
                    backends.pcm_samples(pcm, encoding, rate)

    def test_output_resamples_to_16k_and_clips_without_wrapping(self):
        source = np.zeros(22050, dtype=np.float32)
        self.assertEqual(len(backends.pcm_bytes(source, 22050)), SAMPLE_RATE * 2)
        pcm = backends.pcm_bytes(np.array([-2, 0, 2], dtype=np.float32), SAMPLE_RATE)
        np.testing.assert_array_equal(np.frombuffer(pcm, dtype="<i2"), [-32767, 0, 32767])

    def test_nonfinite_or_non_mono_model_output_fails(self):
        for samples in (np.array([]), np.array([np.nan]), np.array([np.inf]), np.zeros((2, 2))):
            with self.subTest(shape=samples.shape):
                with self.assertRaises(ValueError):
                    backends.pcm_bytes(samples, SAMPLE_RATE)

    def test_runtime_does_not_depend_on_torch(self):
        self.assertIsNone(importlib.util.find_spec("torch"))


class StreamingAsrTest(unittest.TestCase):
    def test_buffered_stream_preserves_audio_order_and_returns_one_final_event(self):
        backend = backends.SenseVoiceASRBackend.__new__(backends.SenseVoiceASRBackend)
        backend.recognize = mock.Mock(return_value={"text": "当前电池电量", "confidence": 0.0})
        events = list(backend.recognize_stream([b"\1\0", b"\2\0", b"\3\0"]))
        backend.recognize.assert_called_once_with(b"\1\0\2\0\3\0")
        self.assertEqual(events, [{"event_type": 1, "text": "当前电池电量",
                                   "confidence": 0.0, "is_final": True}])

    def test_stream_rejects_odd_or_excessive_chunks_before_inference(self):
        backend = backends.SenseVoiceASRBackend.__new__(backends.SenseVoiceASRBackend)
        backend.recognize = mock.Mock()
        for chunks in ([b"\1"], [b"\0" * (backends.MAX_AUDIO_SECONDS * 32000 + 2)]):
            with self.assertRaises(ValueError):
                list(backend.recognize_stream(chunks))
        backend.recognize.assert_not_called()

    def test_one_shot_and_stream_instances_share_cpu_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("model.int8.onnx", "tokens.txt"):
                (directory / name).touch()
            with mock.patch.dict(os.environ, {"GO2_SPEECH_ASR_MODEL_DIR": temporary,
                                              "GO2_SPEECH_THREADS": "2"}), \
                    mock.patch.object(backends, "_asr_engines", {}), \
                    mock.patch.object(backends.sherpa_onnx.OfflineRecognizer, "from_sense_voice") as factory:
                one = backends.SenseVoiceASRBackend()
                streaming = backends.SenseVoiceASRBackend()
                self.assertIs(one._engine, streaming._engine)
                self.assertIs(one._lock, streaming._lock)
                factory.assert_called_once()
                self.assertEqual(factory.call_args.kwargs["provider"], "cpu")


class TtsContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_returns_ordered_even_pcm_frames(self):
        backend = backends.VitsTTSBackend.__new__(backends.VitsTTSBackend)
        pcm = np.arange(16000, dtype="<i2").tobytes()
        backend.synthesize = mock.AsyncMock(return_value=pcm)
        frames = [frame async for frame in backend.synthesize_stream("电量")]
        self.assertEqual(b"".join(frames), pcm)
        self.assertTrue(all(len(frame) <= 3200 and len(frame) % 2 == 0 for frame in frames))

    async def test_tts_invalid_text_voice_and_speed_never_run_model(self):
        backend = backends.VitsTTSBackend.__new__(backends.VitsTTSBackend)
        backend._engine = mock.Mock(num_speakers=1)
        for text, voice, speed in (
            ("", "", 1.0), ("x" * 513, "", 1.0),
            ("电量", "", 0), ("电量", "", float("nan")),
            ("电量", "not-a-speaker", 1.0), ("电量", "2", 1.0),
        ):
            with self.subTest(voice=voice, speed=speed):
                with self.assertRaises(TTSInputError):
                    await backend.synthesize(text, voice, speed)
        backend._engine.generate.assert_not_called()


class ModelArchiveTest(unittest.TestCase):
    def test_asr_extraction_stops_before_unused_tail_after_runtime_and_license(self):
        members = []
        for name in ("LICENSE", "model.int8.onnx", "tokens.txt"):
            member = tarfile.TarInfo("release/" + name)
            member.size = 2
            members.append(member)

        def required_then_unreadable_tail():
            yield from members
            raise AssertionError("ASR extraction traversed unused archive tail")

        archive = mock.MagicMock()
        archive.__iter__.side_effect = required_then_unreadable_tail
        archive.extractfile.side_effect = lambda member: io.BytesIO(b"ok")
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(models.tarfile, "open") as opened:
            opened.return_value.__enter__.return_value = archive
            models.extract_model(Path(temporary) / "unused.tar.bz2", Path(temporary) / "out", "asr")
            for name in ("LICENSE", "model.int8.onnx", "tokens.txt"):
                self.assertEqual((Path(temporary) / "out" / name).read_bytes(), b"ok")

    def test_extract_omits_unused_float32_asr_weights(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "model.tar.bz2"
            with tarfile.open(archive, "w:bz2") as bundle:
                for name in ("model.onnx", "model.int8.onnx", "tokens.txt"):
                    member = tarfile.TarInfo("release/" + name)
                    member.size = 2
                    bundle.addfile(member, io.BytesIO(b"ok"))
            models.extract_model(archive, root / "out", "asr")
            self.assertFalse((root / "out/model.onnx").exists())
            self.assertEqual((root / "out/model.int8.onnx").read_bytes(), b"ok")

    def test_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "unsafe.tar.bz2"
            with tarfile.open(archive, "w:bz2") as bundle:
                member = tarfile.TarInfo("../escape")
                member.size = 2
                bundle.addfile(member, io.BytesIO(b"no"))
            with self.assertRaises(ValueError):
                models.extract_model(archive, root / "out", "tts")
            self.assertFalse((root / "escape").exists())


if __name__ == "__main__":
    unittest.main()
