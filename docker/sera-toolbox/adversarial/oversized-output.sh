#!/usr/bin/env bash
# New files in the output bind must be deleted when over the cap.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/out"
export SERA_SANDBOX_OUT="$tmp/out"
export SERA_SANDBOX_OUT_MAX_BYTES=16
export SERA_SANDBOX_OUT_MAX_FILES=256

set +e
convert rose: "$tmp/out/x.png" >stdout 2>stderr
status=$?
set -e
if [ "$status" -eq 3 ] && [ ! -f "$tmp/out/x.png" ] && grep -q 'oversized output' stderr; then
  echo "OK oversized output refused and removed"
  exit 0
fi
echo "FAIL oversized output (exit=$status exists=$(test -f "$tmp/out/x.png" && echo yes || echo no))" >&2
cat stderr >&2 || true
exit 1
