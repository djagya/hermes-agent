#!/usr/bin/env bash
# High-ratio 7z must be refused before extract. Uses PATH 7z (wrap)
# so check-archive-members sees the origin binary, not this wrapper.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
python3 -c 'open("zeros.bin","wb").write(b"\x00" * (8 * 1024 * 1024))'
# Create may go through wrap; member check on zeros.bin is a no-op.
set +e
7z a -mx=9 bomb.7z zeros.bin >mk 2>&1
mkstatus=$?
set -e
if [ "$mkstatus" -ne 0 ] || [ ! -f bomb.7z ]; then
  echo "FAIL could not create 7z bomb (exit=$mkstatus)" >&2
  cat mk >&2 || true
  exit 1
fi

set +e
7z l bomb.7z >out 2>err
status=$?
set -e
if [ "$status" -ne 0 ] && grep -qi 'bomb\|zip-slip\|refusing' err; then
  echo "OK high-ratio 7z refused"
  exit 0
fi
echo "FAIL high-ratio 7z accepted (exit=$status)" >&2
cat err >&2 || true
exit 1
