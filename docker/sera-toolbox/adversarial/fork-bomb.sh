#!/usr/bin/env bash
# Process cap must stop a wrap-child fork bomb (ulimit -u, no --unshare-pid).
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
export SERA_SANDBOX_OUT="$tmp"
export SERA_SANDBOX_NPROC=8
export SERA_SANDBOX_TIMEOUT=10

set +e
sera-nproc-probe 80 >out 2>err
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo "FAIL process cap allowed 80 children" >&2
  cat err >&2 || true
  exit 1
fi
if ! grep -q 'fork failed after' err; then
  echo "FAIL process cap did not refuse forks (exit=$status)" >&2
  cat err >&2 || true
  exit 1
fi
echo "OK process cap blocked fork bomb (exit=$status)"
