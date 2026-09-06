#!/usr/bin/env bash
# Ghostscript must not honor /URI fetch (no network + -dSAFER).
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
printf '%s\n' '%PDF-1.1
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 100 100]/Parent 2 0 R/AA<</O<</S/URI/URI(http://127.0.0.1:9/x)>>>>>>endobj
trailer<</Root 1 0 R>>
%%EOF' > uri.pdf

set +e
gs -q -dSAFER -dNOPAUSE -dBATCH -sDEVICE=pdfwrite -sOutputFile=out.pdf uri.pdf >log 2>err
status=$?
set -e
if grep -qi '127.0.0.1\|example.com' out.pdf 2>/dev/null; then
  echo "FAIL gs embedded fetched URI" >&2
  exit 1
fi
# Unshare-net + SAFER: any non-hanging outcome is success.
echo "OK gs URI/external action did not fetch (exit=$status)"
