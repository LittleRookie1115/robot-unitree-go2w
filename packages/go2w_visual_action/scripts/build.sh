#!/usr/bin/env bash
set -euo pipefail
package="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="$(cd "$package/../.." && pwd)"
source "$root/native/env.sh"
build="$package/rbnx-build"
python="$build/venv/bin/python"
mkdir -p "$build/tmp" "$build/uv-cache"
export TMPDIR="$build/tmp"
export UV_CACHE_DIR="$build/uv-cache"
if [[ ! -x "$python" ]]; then
  uv venv --python "$GO2_SDK_PYTHON" "$build/venv"
fi
uv_args=(pip install --python "$python")
if [[ -n "${GO2W_OFFLINE_WHEELHOUSE:-}" ]]; then
  [[ -d "$GO2W_OFFLINE_WHEELHOUSE" ]] || {
    echo "GO2W_OFFLINE_WHEELHOUSE must name an existing directory." >&2
    exit 2
  }
  uv_args+=(--no-index --find-links "$GO2W_OFFLINE_WHEELHOUSE")
fi
uv "${uv_args[@]}" \
  -e "$ROBONIX_SOURCE_PATH/pylib/robonix-api" -e "$package"
rbnx codegen --mcp -p "$package"
