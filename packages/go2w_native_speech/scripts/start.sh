#!/usr/bin/env bash
set -euo pipefail
package="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="$(cd "$package/../.." && pwd)"
workspace="$(dirname "$root")"
build="${GO2_SPEECH_BUILD:-$root/rbnx-build/native/speech}"
official="${ROBONIX_SOURCE_PATH:-$workspace/robonix}"
export PYTHONNOUSERSITE=1
export PYTHONPATH="$package:$official/services/speech:$package/rbnx-build/codegen/proto_gen:$package/rbnx-build/codegen/robonix_mcp_types"
export SPEECH_BACKEND=custom
export SPEECH_ASR_BACKEND_CLASS=go2w_native_speech.backends:SenseVoiceASRBackend
export SPEECH_ASR_STREAM_BACKEND_CLASS=go2w_native_speech.backends:SenseVoiceASRBackend
export SPEECH_TTS_BACKEND_CLASS=go2w_native_speech.backends:VitsTTSBackend
export SPEECH_TTS_OUTPUT_ENCODING=pcm_s16le
export SPEECH_TTS_OUTPUT_SAMPLE_RATE=16000
export TTS_VOICE="${GO2_SPEECH_TTS_SPEAKER:-4}"
exec "$build/venv/bin/python" -m speech_service.service
