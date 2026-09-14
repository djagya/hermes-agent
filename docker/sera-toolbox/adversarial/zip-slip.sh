#!/usr/bin/env bash
# Zip-slip fixture: wrapper must refuse a zip whose members escape.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
python3 - <<'PY'
import zipfile
with zipfile.ZipFile("slip.zip", "w") as z:
    z.writestr("../escape.txt", "evil")
PY

set +e
unzip -o slip.zip >out 2>err
status=$?
set -e
if [ "$status" -eq 2 ] && grep -q 'parent-traversal\|zip-slip' err; then
  echo "OK zip-slip member refused"
  exit 0
fi
echo "FAIL zip-slip not refused (exit=$status)" >&2
cat err >&2 || true
exit 1
