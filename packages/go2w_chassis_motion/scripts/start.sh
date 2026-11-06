#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
operator_motion_gate="${GO2_ALLOW_MOTION:-false}"
source "$root/native/env.sh"
export GO2_ALLOW_MOTION="$operator_motion_gate"
package="$root/packages/go2w_chassis_motion"
mcp_gen="$package/rbnx-build/codegen/robonix_mcp_types"
export PYTHONPATH="$package:$package/rbnx-build/codegen/proto_gen:$mcp_gen"
exec "$GO2_SDK_PYTHON" -m go2w_chassis_motion.provider
