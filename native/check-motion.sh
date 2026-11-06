#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
exec "$GO2_SDK_PYTHON" "$GO2_NATIVE_ROOT/native/check_motion.py" "$@"
