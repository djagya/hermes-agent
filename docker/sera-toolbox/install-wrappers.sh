#!/usr/bin/env bash
# Expose PATH wrappers. Real bits stay where the distro put them when
# they are scripts that locate siblings via $0 (soffice). Binaries that
# live in /usr/local/bin are copied to libexec first so replacing the
# PATH entry does not delete the tool.
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
  if [ "$(readlink -f "$src")" = "$(readlink -f "$wrap")" ]; then
    continue
  fi
  resolved="$(readlink -f "$src")"
  if [ ! -e "$resolved" ]; then
    echo "sera-toolbox: skip broken ${t} -> ${src}" >&2
    missing=$((missing + 1))
    continue
  fi
  # Replacing /usr/local/bin/T would delete the real binary; copy first.
  if [ "$src" = "/usr/local/bin/${t}" ]; then
    cp -L -p "$resolved" "${libexec}/${t}"
    rm -f "${libexec}/${t}.origin"
  else
    # Distro path (e.g. /usr/bin/soffice -> program/soffice). Keep it;
    # a libexec copy breaks $0-relative LibreOffice.
    printf '%s\n' "$resolved" > "${libexec}/${t}.origin"
    rm -f "${libexec}/${t}"
  fi
  ln -sfn "$wrap" "/usr/local/bin/${t}"
  installed=$((installed + 1))
done

echo "sera-toolbox: wrapped=${installed} missing=${missing}"
