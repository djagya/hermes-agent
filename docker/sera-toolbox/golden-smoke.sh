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
HTML(string="<html><body><p>Здравствуй golden 😀</p></body></html>").write_pdf("unicode.pdf")
import zipfile
with zipfile.ZipFile("golden.zip", "w") as z:
    z.writestr("hello.txt", "ok\n")
PY

pdftotext golden.pdf golden.txt
grep -q golden golden.txt
pdftotext unicode.pdf unicode.txt
grep -q 'Здравствуй' unicode.txt
test "$(wc -c < unicode.pdf)" -gt "$(wc -c < golden.pdf)"

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

python3 - <<'PY'
from PIL import Image, ImageDraw, ImageFont

im = Image.new("RGB", (420, 90), "white")
draw = ImageDraw.Draw(im)
font = ImageFont.truetype(
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 48
)
draw.text((12, 18), "GOLDEN", fill="black", font=font)
im.save("scan.png")
print("OK golden scan.png")
PY
tesseract scan.png stdout -l eng | grep -qi GOLDEN
convert scan.png scan-in.pdf
timeout 90s ocrmypdf --language eng --force-ocr scan-in.pdf scan-ocr.pdf
pdftotext scan-ocr.pdf - | grep -qi GOLDEN
echo "OK ocrmypdf searchable pdf"

printf 'not a sqlite database\n' > bad.db
if sqlite3 bad.db 'PRAGMA integrity_check' >/dev/null 2>&1; then
  echo "malformed sqlite was accepted" >&2
  exit 1
fi

echo "OK golden heif/wav/docx/xlsx/ocr/bad-sqlite"
