#!/usr/bin/env bash
# Archive wrapper refuses member-count bombs before extract.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
export SERA_SANDBOX_ZIP_MAX_MEMBERS=8
python3 - <<'PY'
import zipfile
with zipfile.ZipFile("many.zip", "w") as z:
    for i in range(32):
        z.writestr(f"n{i}.txt", b"x")
PY

set +e
unzip -l many.zip >out 2>err
status=$?
set -e
if [ "$status" -ne 0 ] && grep -qi 'bomb\|zip-slip\|refusing' err; then
  echo "OK member-count zip refused"
  exit 0
fi
echo "FAIL member-count zip accepted (exit=$status)" >&2
cat err >&2 || true
exit 1
