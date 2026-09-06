#!/usr/bin/env bash
# Image-toolbox smoke. Run inside the built image.
set -euo pipefail

need=(
  bwrap file jq sqlite3 zip unzip 7z zstd
  pdftotext qpdf gs tesseract
  convert pandoc soffice
  ffmpeg ffprobe exiftool
  shellcheck ruff
  python3
)

fail=0
for c in "${need[@]}"; do
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "MISSING $c" >&2
    fail=1
  else
    echo "OK $c -> $(command -v "$c")"
  fi
done

# Wrappers must not be the raw /usr/bin copy.
if [ "$(command -v pdftotext)" != "/usr/local/bin/pdftotext" ]; then
  echo "pdftotext not wrapped: $(command -v pdftotext)" >&2
  fail=1
fi

python3 - <<'PY'
import importlib
for name in ("fitz", "weasyprint", "yt_dlp"):
    importlib.import_module(name)
    print("OK py", name)
PY

exit "$fail"
