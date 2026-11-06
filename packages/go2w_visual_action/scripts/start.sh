#!/usr/bin/env bash
set -euo pipefail
package="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="$(cd "$package/../.." && pwd)"
source "$root/native/env.sh"
chassis="$root/packages/go2_chassis_motion"
native="$root/packages/go2_native"
export PYTHONPATH="$package:$package/rbnx-build/codegen/proto_gen:$package/rbnx-build/codegen/robonix_mcp_types:$chassis/rbnx-build/codegen/proto_gen:$chassis/rbnx-build/codegen/robonix_mcp_types:$native/rbnx-build/codegen/proto_gen:$native/rbnx-build/codegen/robonix_mcp_types${PYTHONPATH:+:$PYTHONPATH}"
export ROBONIX_ATLAS="${ROBONIX_ATLAS:-127.0.0.1:50051}"
cd "$package"
exec "$package/rbnx-build/venv/bin/python" -m go2w_visual_action.main
