#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
[[ -f "$GO2_NATIVE_BUILD/soma.yaml" ]] || {
  echo "Run bash native/build.sh first." >&2; exit 2;
}
[[ -d /sys/class/net/eth10 && "$(< /sys/class/net/eth10/carrier)" == 1 ]] || {
  echo "The measured eth10 sensor link is unavailable." >&2; exit 2;
}
mkdir -p "$GO2_NATIVE_ROOT/.run"
exec {native_lock}>"$GO2_NATIVE_ROOT/.run/native.lock"
flock -n "$native_lock" || { echo "Native deployment is already running." >&2; exit 73; }
if ! "$GO2_SDK_PYTHON" "$GO2_NATIVE_ROOT/native/check_vlm.py"; then
  echo "[VLM] Robonix will still start; model tasks require a reachable model API." >&2
  echo "[VLM] Recheck with: bash $GO2_NATIVE_ROOT/native/check-vlm.sh" >&2
fi
cd "$GO2_NATIVE_ROOT/native"
exec rbnx boot --no-update-check -f "$PWD/robonix_manifest.yaml"
