#!/usr/bin/env bash
# Tiny workflow fixtures: presence of a tool is not enough.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
export SERA_SANDBOX_OUT="$tmp"

python3 - <<'PY'
from pathlib import Path
Path("hello.md").write_text("# hi\n\nfixture\n", encoding="utf-8")
from weasyprint import HTML
HTML(string="<html><body><p>golden</p></body></html>").write_pdf("golden.pdf")
import zipfile
with zipfile.ZipFile("golden.zip", "w") as z:
    z.writestr("hello.txt", "ok\n")
PY

pdftotext golden.pdf golden.txt
grep -q golden golden.txt

unzip -l golden.zip | grep -q hello.txt
zstd -q golden.zip -o golden.zip.zst
zstd -dq golden.zip.zst -o golden.zip.out
cmp -s golden.zip golden.zip.out

7z a -bd -y golden.7z hello.md >/dev/null
7z t golden.7z >/dev/null

python3 - <<'PY'
import fitz
doc = fitz.open("golden.pdf")
text = "".join(page.get_text() for page in doc)
assert "golden" in text.lower()
print("OK golden pdf/zip/zstd/7z/pymupdf")
PY
