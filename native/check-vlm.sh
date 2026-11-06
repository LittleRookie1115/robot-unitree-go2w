#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh" || exit 2
exec "$GO2_SDK_PYTHON" "$GO2_NATIVE_ROOT/native/check_vlm.py" "$@"
