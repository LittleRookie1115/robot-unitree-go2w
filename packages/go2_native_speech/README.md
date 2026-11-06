# Native CPU backends for official Robonix Speech

This package runs the official `speech_service.service` using its custom
backend API. It provides real sherpa-onnx CPU inference on the native ARM64
host. It installs no Torch, CUDA or JetPack Python bindings, and does not invoke
the upstream heavy Speech build script.

The wrapper manifest is `package_manifest.yaml`. Configure the deployment's
speech service with:

```yaml
speech_backend: custom
speech_asr_backend_class: go2_native_speech.backends:SenseVoiceASRBackend
speech_asr_stream_backend_class: go2_native_speech.backends:SenseVoiceASRBackend
speech_tts_backend_class: go2_native_speech.backends:VitsTTSBackend
tts_voice: "4"
wake_word_model_dir: /home/unitree/robonix_ws/robot-unitree-go2-native/rbnx-build/native/speech/models/kws
```

`config.example.yaml` contains this configuration. The main deployment owns
the speaker target and the laptop audio bridge. This package neither starts
that bridge nor submits Liaison/Pilot tasks or plays audio.

Build from the robot repository:

```bash
bash native/build-voice.sh
```

The build creates `rbnx-build/native/speech/venv` using managed Python 3.11,
installs `robonix-api` editable and the official Speech base dependencies,
and uses the official Speech source through `PYTHONPATH`. The upstream source
repository remains unchanged. Codegen runs against this wrapper's manifest
of the official Speech contracts; both protobuf and MCP dataclasses stay
under this package's ignored `rbnx-build/codegen/`.

The start script exports the three custom class selectors and explicitly sets
`SPEECH_TTS_OUTPUT_ENCODING=pcm_s16le` and
`SPEECH_TTS_OUTPUT_SAMPLE_RATE=16000`. It executes the official Speech module
with the isolated interpreter. There is no fallback to mock, GPU or cloud
inference. Models must already exist before service initialization.

## Models and provenance

Model bundles come from the official `k2-fsa/sherpa-onnx` GitHub releases:

| Purpose | Asset | Installed files |
| --- | --- | --- |
| ASR | `asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2` | `models/sensevoice/model.int8.onnx`, tokens and original notices |
| TTS | `tts-models/sherpa-onnx-vits-zh-ll.tar.bz2` | `models/vits-zh-ll/`, including text normalization FSTs |
| Optional KWS | `kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2` | `models/kws/` for the official Speech KWS backend |

The downloader queries `api.github.com` and downloads release assets through
the API's octet-stream endpoint, which can redirect to the official asset CDN.
It never changes host DNS or proxy settings. The SenseVoice bundle includes
unused float32 weights; these are not extracted into the runtime model
directory. Each model's `provenance.json` records the asset ID, size, SHA-256,
upstream digest when published, and hashes of required runtime files.
For older assets with no published digest, the recorded SHA-256 is locally
computed provenance, not an independently published checksum.

Archives, models and reports stay in the ignored
`rbnx-build/native/speech/`. Rebuilding verifies required model files rather
than downloading again. `--only asr`, `--only tts`, `--only kws` and `--no-kws`
can be passed to the build/downloader.

## Runtime contract

ASR accepts 16 kHz mono `pcm_s16le`, bounded to 120 seconds. One-shot and
streaming instances share one locked SenseVoice CPU engine. Streaming buffers
audio until input closes and yields one final transcript; it does not claim
incremental partial decoding. SenseVoice's API provides no calibrated
confidence here, so `confidence=0.0` means unavailable, not a measured zero
probability.

TTS accepts nonempty text up to 512 characters, numeric VITS speaker IDs
(default 4, selected using the saved real speaker comparison),
and speed factors 0.5–2.0. Output is resampled when necessary and returned as
16 kHz mono little-endian PCM. One-shot audio stays below the default gRPC
message limit. TTS streaming currently generates a bounded utterance first
and then yields even 100 ms PCM frames. VITS synthesis is serialized and
honors the official request-active callback before/after inference and while
yielding frames.

`GO2_SPEECH_THREADS` defaults to 2; `GO2_SPEECH_TTS_SPEAKER` overrides the
default voice ID. Model-path overrides are
`GO2_SPEECH_ASR_MODEL_DIR` and `GO2_SPEECH_TTS_MODEL_DIR`; the default build root
may be changed through `GO2_SPEECH_BUILD`. No credentials file is read.

KWS weights may be loaded by the official Speech service using the documented
config path. Downloading them does not enable handsfree operation. No
wake-word-to-Pilot task is part of this package's checks.

## Verification

The build runs boundary/format, buffered streaming, shared-engine and archive
tests. For actual model inference without a server, playback or robot task:

```bash
package="$PWD/packages/go2_native_speech"
official="$(dirname "$PWD")/robonix"
PYTHONPATH="$package:$official/services/speech:$package/rbnx-build/codegen/proto_gen:$package/rbnx-build/codegen/robonix_mcp_types" \
  rbnx-build/native/speech/venv/bin/python -m go2_native_speech.verify
```

This loads custom classes through the official Speech initializer, synthesizes
“请告诉我当前电池电量”, and passes the actual resulting PCM through its one-shot
and streaming ASR servicers. It writes `inference-evidence.json` and
`tts-asr-roundtrip.wav` in the ignored build root, with actual timings,
real-time factors, transcript error rates, model hashes and audio measurements.
No network RPC, speaker playback or Liaison/Pilot submission is used.
Laptop microphone, WebSocket transport, speaker routing and browser latency
remain the main deployment's integration checks.
