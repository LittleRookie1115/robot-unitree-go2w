#!/usr/bin/env bash
# All native artifacts belong to this checkout; host networking stays unchanged.
export GO2_NATIVE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GO2_WORKSPACE="$(dirname "$GO2_NATIVE_ROOT")"
source "$GO2_WORKSPACE/env.sh"
source "$GO2_NATIVE_ROOT/native/vlm-env.sh"
load_robonix_vlm_env "$HOME/.config/robonix/env" || return
export GO2W_JEV_MODEL_URL="${GO2W_JEV_MODEL_URL:-http://127.0.0.1:8765}"
export GO2_NATIVE_BUILD="$GO2_NATIVE_ROOT/rbnx-build/native"
export GO2_SDK_PYTHON="$GO2_WORKSPACE/.tools/rbnx-python/bin/python"
export RBNX_CODEGEN_PYTHON="$GO2_SDK_PYTHON"
export ROS_DOMAIN_ID=0
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI="$GO2_NATIVE_ROOT/native/cyclonedds.xml"
export ROBONIX_DEPLOY_DIR="$GO2_NATIVE_ROOT"
export ROBONIX_SOURCE_PATH="$GO2_WORKSPACE/robonix"
export ROBONIX_PROVIDER_BIND_HOST=127.0.0.1
export ROBONIX_ADVERTISE_HOST=127.0.0.1
export GO2_ALLOW_MOTION=false
