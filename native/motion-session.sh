#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
mkdir -p "$GO2_NATIVE_ROOT/.run"
exec {native_lock}>"$GO2_NATIVE_ROOT/.run/native.lock"
flock -n "$native_lock" || { echo "A native deployment is already running." >&2; exit 73; }
exec "$GO2_SDK_PYTHON" "$GO2_NATIVE_ROOT/native/motion_session.py" "$@"
