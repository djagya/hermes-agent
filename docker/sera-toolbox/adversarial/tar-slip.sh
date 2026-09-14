#!/usr/bin/env bash
# Tar-slip fixture: wrapper must refuse a tar whose members escape.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
python3 - <<'PY'
import tarfile
from io import BytesIO

payload = BytesIO(b"evil")
info = tarfile.TarInfo(name="../escape.txt")
info.size = 4
with tarfile.open("slip.tar", "w") as t:
    t.addfile(info, payload)
PY

extractor=""
for c in bsdtar 7z; do
  if command -v "$c" >/dev/null 2>&1; then
    extractor="$c"
    break
  fi
done
if [ -z "$extractor" ]; then
  echo "FAIL neither bsdtar nor 7z on PATH" >&2
  exit 1
fi

set +e
if [ "$extractor" = 7z ]; then
  7z x slip.tar >out 2>err
else
  bsdtar -xf slip.tar >out 2>err
fi
status=$?
set -e
if [ "$status" -eq 2 ] && grep -q 'parent-traversal\|zip-slip' err; then
  echo "OK tar-slip member refused via $extractor"
  exit 0
fi
echo "FAIL tar-slip not refused via $extractor (exit=$status)" >&2
cat err >&2 || true
exit 1
