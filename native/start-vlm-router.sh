#!/usr/bin/env bash
# Start the loopback VLM router. Kept separate from start.sh so the router is
# infrastructure, not a Robonix manifest service.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROBONIX_VLM_ROUTER_CONFIG="${ROBONIX_VLM_ROUTER_CONFIG:-$HOME/.config/robonix-vlm-router/config.json}"
exec /usr/bin/python3 "$root/native/vlm_router.py" --config "$ROBONIX_VLM_ROUTER_CONFIG"
