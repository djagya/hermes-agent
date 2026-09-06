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
  gh gitleaks tirith rclone op himalaya
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
for name in ("fitz", "weasyprint", "yt_dlp", "ddgs", "fal_client", "faster_whisper"):
    importlib.import_module(name)
    print("OK py", name)
try:
    import pillow_heif
    print("OK py pillow_heif")
except ImportError:
    print("MISSING py pillow_heif")
    raise SystemExit(1)
PY

if [ ! -f /etc/hermes/config.yaml ]; then
  echo "MISSING /etc/hermes/config.yaml managed policy" >&2
  fail=1
fi

if ! hermes-image-info --json >/tmp/image-info.json; then
  echo "hermes-image-info failed" >&2
  fail=1
fi
if ! hermes-image-doctor; then
  echo "hermes-image-doctor failed" >&2
  fail=1
fi

# Final stage must not ship compilers or docker-cli.
if command -v gcc >/dev/null 2>&1; then
  echo "gcc must not be in runtime image: $(command -v gcc)" >&2
  fail=1
fi
if command -v docker >/dev/null 2>&1; then
  echo "docker must not be in runtime image: $(command -v docker)" >&2
  fail=1
fi

if [ ! -x /usr/local/bin/himalaya.real ]; then
  echo "MISSING /usr/local/bin/himalaya.real" >&2
  fail=1
fi

# sqlite CLI must be the fixed 3.53 build, not Debian 3.46.
sqlite_ver="$(sqlite3 -version | awk '{print $1}')"
case "$sqlite_ver" in
  3.53.*) echo "OK sqlite $sqlite_ver" ;;
  *) echo "BAD sqlite $sqlite_ver (want 3.53.x)" >&2; fail=1 ;;
esac

if ! grep -q 'snapshot.debian.org/archive/debian/20260508T000000Z' \
      /etc/apt/sources.list.d/debian.sources; then
  echo "apt sources not pinned to Debian snapshot 20260508T000000Z" >&2
  fail=1
fi

case "${XDG_CACHE_HOME:-}:${UV_CACHE_DIR:-}:${HF_HOME:-}" in
  /opt/data/cache:/opt/data/cache/uv:/opt/data/cache/huggingface)
    echo "OK cache roots" ;;
  *)
    echo "BAD cache roots XDG_CACHE_HOME=${XDG_CACHE_HOME:-} UV_CACHE_DIR=${UV_CACHE_DIR:-} HF_HOME=${HF_HOME:-}" >&2
    fail=1 ;;
esac

exit "$fail"
