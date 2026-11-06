#!/usr/bin/env bash
set -euo pipefail
package="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="$(cd "$package/../.." && pwd)"
workspace="$(dirname "$root")"
build="${GO2_SPEECH_BUILD:-$root/rbnx-build/native/speech}"
uv="$workspace/.tools/uv-package/bin/uv"
base_python="$workspace/.tools/python/cpython-3.11-linux-aarch64-gnu/bin/python3.11"
official="${ROBONIX_SOURCE_PATH:-$workspace/robonix}"
mkdir -p "$build"
export UV_CACHE_DIR="$build/cache/uv"
export PYTHONNOUSERSITE=1
export PATH="$workspace/.tools/bin:$PATH"
if [[ ! -x "$build/venv/bin/python" ]]; then
  "$uv" venv --python "$base_python" "$build/venv"
fi
mapfile -t speech_dependencies < <("$base_python" - "$official/services/speech/pyproject.toml" <<'PY'
import sys, tomllib
from pathlib import Path
project = tomllib.loads(Path(sys.argv[1]).read_text())["project"]
for requirement in project["dependencies"]:
    if requirement != "robonix-api":
        print(requirement)
PY
)
"$uv" pip install --python "$build/venv/bin/python" \
  -e "$official/pylib/robonix-api" -e "$package" "${speech_dependencies[@]}"
export ROBONIX_HOME="${ROBONIX_HOME:-$workspace/.robonix}"
export RBNX_CODEGEN_PYTHON="$build/venv/bin/python"
"$workspace/.tools/bin/rbnx" codegen -p "$package" --mcp --python "$build/venv/bin/python"
export PYTHONPATH="$package:$official/services/speech:$package/rbnx-build/codegen/proto_gen:$package/rbnx-build/codegen/robonix_mcp_types"
"$build/venv/bin/python" -m go2_native_speech.models "$@"
"$build/venv/bin/python" -m unittest discover -s "$package/tests" -v
echo "Native CPU Speech ready; no server, playback or robot action was started."
