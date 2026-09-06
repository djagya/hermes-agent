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

python3 - <<'PY'
import wave

from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()
Image.new("RGB", (16, 16), (200, 40, 40)).save("golden.heif", format="HEIF")

with wave.open("golden.wav", "w") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b"\x00\x00" * 800)
print("OK golden heif/wav written")
PY

exiftool -s -s -s -FileType golden.heif | grep -Ei 'heif|heic'
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 golden.wav >/dev/null

printf 'golden-docx\n' > golden-docx.md
timeout 90s pandoc golden-docx.md -o golden.docx
mkdir -p docx-out
timeout 120s soffice --headless --nologo --nolockcheck --norestore \
  --convert-to pdf --outdir docx-out golden.docx >/dev/null
pdftotext docx-out/golden.pdf - | grep -q golden-docx

printf 'label,value\ngolden-xlsx,1\n' > golden.csv
mkdir -p xlsx-out
timeout 120s soffice --headless --nologo --nolockcheck --norestore \
  --convert-to xlsx --outdir xlsx-out golden.csv >/dev/null
test -f xlsx-out/golden.xlsx

echo "OK golden heif/wav/docx/xlsx"
