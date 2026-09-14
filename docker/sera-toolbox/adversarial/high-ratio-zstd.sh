#!/usr/bin/env bash
# High-ratio tar.zst must be refused before extract. GNU tar is
# unwrapped; zstd is unsandboxed; listing goes through wrap bsdtar.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
python3 -c 'open("zeros.bin","wb").write(b"\x00" * (8 * 1024 * 1024))'
tar -cf zeros.tar zeros.bin
zstd -q zeros.tar
test -f zeros.tar.zst

set +e
bsdtar -tvf zeros.tar.zst >out 2>err
status=$?
set -e
if [ "$status" -ne 0 ] && grep -qi 'bomb\|zip-slip\|refusing' err; then
  echo "OK high-ratio tar.zst refused"
  exit 0
fi
echo "FAIL high-ratio tar.zst accepted (exit=$status)" >&2
cat err >&2 || true
exit 1
