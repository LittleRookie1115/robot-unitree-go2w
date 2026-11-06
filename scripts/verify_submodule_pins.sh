#!/usr/bin/env bash
# Fail closed unless vendored third-party snapshots match third_party/PINS.
# The native deployment stores these sources as ordinary Git trees. A gitlink
# would mean the pin file and the index no longer describe the same thing.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pins="$ROOT/third_party/PINS"

[[ -f "$pins" ]] || {
  echo "missing vendored source pins: third_party/PINS" >&2
  exit 1
}
[[ ! -e "$ROOT/.gitmodules" ]] || {
  echo "root .gitmodules conflicts with vendored third_party snapshots" >&2
  exit 1
}

count=0
while IFS=$'\t' read -r path url tree; do
  [[ -z "${path:-}" || "$path" == \#* ]] && continue
  [[ "$path" == third_party/* && "$url" == https://* && "$tree" =~ ^[0-9a-f]{40}$ ]] || {
    echo "malformed source pin: $path" >&2
    exit 1
  }
  entry="$(git -C "$ROOT" ls-tree HEAD -- "$path")"
  [[ -n "$entry" ]] || {
    echo "missing vendored source: $path" >&2
    exit 1
  }
  read -r mode kind object _ <<<"$entry"
  [[ "$mode" == 040000 && "$kind" == tree && "$object" == "$tree" ]] || {
    echo "vendored tree mismatch: $path expected=$tree actual=${object:-missing} mode=${mode:-missing} kind=${kind:-missing}" >&2
    exit 1
  }
  [[ -d "$ROOT/$path" ]] || {
    echo "vendored source is not checked out: $path" >&2
    exit 1
  }
  dirty="$(git -C "$ROOT" status --porcelain -- "$path")"
  [[ -z "$dirty" ]] || {
    echo "vendored source differs from its pinned tree: $path" >&2
    printf '%s\n' "$dirty" >&2
    exit 1
  }
  printf '[source-pin] %s %s\n' "$path" "$object"
  count=$((count + 1))
done <"$pins"

[[ "$count" -eq 4 ]] || {
  echo "expected 4 vendored source pins, found $count" >&2
  exit 1
}

echo '[source-pin] PASS'
