#!/usr/bin/env bash
# High-ratio zip must be refused before extract.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
python3 - <<'PY'
import zipfile
with zipfile.ZipFile("bomb.zip", "w", compression=zipfile.ZIP_DEFLATED) as z:
    z.writestr("zeros.bin", b"\x00" * (8 * 1024 * 1024))
PY

set +e
unzip -l bomb.zip >out 2>err
status=$?
set -e
if [ "$status" -ne 0 ] && grep -qi 'bomb\|zip-slip\|refusing' err; then
  echo "OK high-ratio zip refused"
  exit 0
fi
echo "FAIL high-ratio zip accepted (exit=$status)" >&2
cat err >&2 || true
exit 1
