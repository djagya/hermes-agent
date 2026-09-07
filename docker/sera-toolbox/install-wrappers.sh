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
  pdftotext pdfinfo pdftoppm pdffonts pdfimages qpdf gs ghostscript
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

# Python parsers: helpers are the real bits. PATH names go through wrap.
# Venv console scripts sit ahead of /usr/local/bin on image PATH, so
# replace those too or WeasyPrint stays unsandboxed.
helpers_dir=/opt/hermes/docker/sera-toolbox/helpers
if [ -d "$helpers_dir" ]; then
  for helper in sera-weasyprint sera-pymupdf sera-nproc-probe; do
    src="${helpers_dir}/${helper}"
    if [ ! -x "$src" ]; then
      echo "sera-toolbox: skip missing helper ${helper}" >&2
      missing=$((missing + 1))
      continue
    fi
    cp -L -p "$src" "${libexec}/${helper}"
    ln -sfn "$wrap" "/usr/local/bin/${helper}"
    installed=$((installed + 1))
  done
  if [ -x "${libexec}/sera-weasyprint" ]; then
    printf '%s\n' "${libexec}/sera-weasyprint" > "${libexec}/weasyprint.origin"
    rm -f "${libexec}/weasyprint"
    ln -sfn "$wrap" /usr/local/bin/weasyprint
    if [ -e /opt/hermes/.venv/bin/weasyprint ]; then
      ln -sfn "$wrap" /opt/hermes/.venv/bin/weasyprint
    fi
    installed=$((installed + 1))
  fi
fi

echo "sera-toolbox: wrapped=${installed} missing=${missing}"
