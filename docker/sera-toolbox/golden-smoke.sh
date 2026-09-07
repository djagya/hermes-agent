#!/usr/bin/env bash
# Tiny workflow fixtures: presence of a tool is not enough.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
export SERA_SANDBOX_OUT="$tmp"

fix="${SERA_GOLDEN_FIXTURES:-/opt/hermes/docker/sera-toolbox/fixtures}"
cp "$fix/hello.md" "$fix/golden-docx.md" "$fix/golden.csv" "$fix/bad.db" \
   "$fix/golden.pdf" "$fix/golden.wav" "$fix/golden.zip" \
   "$fix/golden.docx" "$fix/golden.xlsx" "$fix/scan.png" "$fix/scan-in.pdf" .

# Renderer proof: wrapped WeasyPrint must still produce a Unicode PDF.
# Stored golden.pdf is the workflow fixture; this one is generated.
printf '%s\n' '<html><body><p>Здравствуй golden 😀</p></body></html>' > unicode.html
weasyprint unicode.html unicode.pdf
test -s unicode.pdf || { echo "FAIL weasyprint wrote empty unicode.pdf" >&2; exit 1; }
echo "OK weasyprint unicode.pdf bytes=$(wc -c < unicode.pdf)"

pdftotext golden.pdf golden.txt || { echo "FAIL pdftotext golden.pdf" >&2; exit 1; }
grep -q golden golden.txt || { echo "FAIL golden.pdf text missing"; cat golden.txt >&2; exit 1; }
echo "OK pdftotext golden.pdf"
pdftotext unicode.pdf unicode.txt || { echo "FAIL pdftotext unicode.pdf" >&2; exit 1; }
grep -q 'Здравствуй' unicode.txt || { echo "FAIL unicode.pdf text missing"; cat unicode.txt >&2; exit 1; }
test "$(wc -c < unicode.pdf)" -gt "$(wc -c < golden.pdf)"
echo "OK pdftotext unicode.pdf"

unzip -l golden.zip | grep -q hello.txt
zstd -q golden.zip -o golden.zip.zst
zstd -dq golden.zip.zst -o golden.zip.out
cmp -s golden.zip golden.zip.out

7z a -bd -y golden.7z hello.md >/dev/null
7z t golden.7z >/dev/null

sera-pymupdf extract golden.pdf | grep -qi golden
echo "OK golden pdf/zip/zstd/7z/pymupdf"

python3 - <<'PY'
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()
Image.new("RGB", (16, 16), (200, 40, 40)).save("golden.heif", format="HEIF")
print("OK golden heif written")
PY

exiftool -s -s -s -FileType golden.heif | grep -Ei 'heif|heic'
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 golden.wav >/dev/null

mkdir -p docx-out
timeout 120s soffice --headless --nologo --nolockcheck --norestore \
  --convert-to pdf --outdir docx-out golden.docx >/dev/null
pdftotext docx-out/golden.pdf - | grep -q golden-docx

test -f golden.xlsx
# Recalc/round-trip the stored workbook, not a CSV rebuild.
mkdir -p xlsx-out
timeout 120s soffice --headless --nologo --nolockcheck --norestore \
  --convert-to csv --outdir xlsx-out golden.xlsx >/dev/null
grep -q golden-xlsx xlsx-out/golden.csv

tesseract scan.png stdout -l eng | grep -qi GOLDEN
# ImageMagick delegates are disabled (no gs). Rebuild scan-in.pdf via
# the wrapped PyMuPDF helper so the baked renderer is still the proof.
sera-pymupdf image-pdf scan.png scan-in.pdf
echo "OK scan-in.pdf via pymupdf"
timeout 90s ocrmypdf --language eng --force-ocr scan-in.pdf scan-ocr.pdf
ocr_txt="$(pdftotext scan-ocr.pdf - || true)"
if ! printf '%s\n' "$ocr_txt" | grep -qi GOLDEN; then
  echo "FAIL ocrmypdf pdftotext missed GOLDEN:" >&2
  printf '%s\n' "$ocr_txt" >&2
  exit 1
fi
echo "OK ocrmypdf searchable pdf"

if sqlite3 bad.db 'PRAGMA integrity_check' >/dev/null 2>&1; then
  echo "malformed sqlite was accepted" >&2
  exit 1
fi

echo "OK golden heif/wav/docx/xlsx/ocr/bad-sqlite"
