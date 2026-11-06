#!/usr/bin/env bash
# Clone the pinned audio bridge into this repository's rbnx cache.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$root/native/deps.lock"
dest="$root/rbnx-boot/cache/primitive-audio-client-bridge-rbnx"
mkdir -p "$(dirname "$dest")"
if [[ -d "$dest/.git" ]]; then
  actual="$(git -C "$dest" rev-parse HEAD)"
  [[ "$actual" == "$AUDIO_CLIENT_BRIDGE_COMMIT" ]] || {
    echo "Audio bridge at $dest is $actual, expected $AUDIO_CLIENT_BRIDGE_COMMIT." >&2
    echo "Remove that directory and run this fetch again." >&2
    exit 2
  }
else
  rm -rf "$dest"
  mkdir -p "$dest"
  git -C "$dest" init
  git -C "$dest" remote add origin "$AUDIO_CLIENT_BRIDGE_URL"
  git -C "$dest" fetch --depth 1 origin "$AUDIO_CLIENT_BRIDGE_COMMIT"
  git -C "$dest" checkout --detach FETCH_HEAD
fi
[[ -f "$dest/package_manifest.yaml" ]] || {
  echo "Pinned audio bridge is missing package_manifest.yaml: $dest" >&2
  exit 2
}
echo "$dest"
