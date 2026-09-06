#!/usr/bin/env bash
# Move parser binaries to libexec and expose PATH wrappers.
set -euo pipefail

libexec=/usr/libexec/sera-toolbox
wrap=/opt/hermes/docker/sera-toolbox/wrap
mkdir -p "$libexec"

# Document/media parsers only. gh/himalaya/rclone/op/yt-dlp stay unsandboxed.
tools=(
  pdftotext pdfinfo pdftoppm qpdf gs ghostscript
  tesseract ocrmypdf
  convert magick identify mogrify
  pandoc soffice
  ffmpeg ffprobe
  exiftool heif-convert heif-info
  unzip 7z 7za
  file
)

installed=0
missing=0
for t in "${tools[@]}"; do
  src="$(command -v "$t" || true)"
  if [ -z "$src" ]; then
    echo "sera-toolbox: skip missing ${t}" >&2
    missing=$((missing + 1))
    continue
  fi
  # Do not wrap an already-wrapped path.
  if [ "$src" = "/usr/local/bin/${t}" ] && [ -L "$src" ]; then
    continue
  fi
  cp -a "$src" "${libexec}/${t}"
  ln -sfn "$wrap" "/usr/local/bin/${t}"
  installed=$((installed + 1))
done

echo "sera-toolbox: wrapped=${installed} missing=${missing}"
