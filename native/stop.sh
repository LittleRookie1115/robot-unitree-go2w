#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
cd "$GO2_NATIVE_ROOT/native"
exec rbnx shutdown -f "$PWD/robonix_manifest.yaml"
