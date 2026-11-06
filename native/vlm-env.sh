#!/usr/bin/env bash
# Source a user-owned VLM configuration without tracing credential values.
load_robonix_vlm_env() {
  local config_file="$1" prior_flags="$-" result=0 name
  set +x
  if [[ -f "$config_file" ]]; then
    unset VLM_BASE_URL VLM_API_KEY VLM_MODEL
    unset ROBONIX_VLM_UPSTREAM ROBONIX_VLM_API_KEY ROBONIX_VLM_MODEL
    set -a
    if ! source "$config_file" >/dev/null 2>&1; then
      echo "Unable to load the Robonix VLM environment file." >&2
      result=1
    else
      for name in VLM_BASE_URL VLM_API_KEY VLM_MODEL; do
        if [[ ! -v "$name" ]] || [[ -z "${!name}" ]]; then
          echo "Missing required VLM configuration variable: $name" >&2
          result=1
        fi
      done
      if (( result == 0 )); then
        export ROBONIX_VLM_UPSTREAM="$VLM_BASE_URL"
        export ROBONIX_VLM_API_KEY="$VLM_API_KEY"
        export ROBONIX_VLM_MODEL="$VLM_MODEL"
      fi
    fi
  fi
  case "$prior_flags" in *a*) ;; *) set +a ;; esac
  case "$prior_flags" in *x*) set -x ;; esac
  return "$result"
}
