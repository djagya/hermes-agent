#!/usr/bin/env bash
# Image-toolbox smoke. Run inside the built image.
set -euo pipefail

need=(
  bwrap file jq sqlite3 zip unzip 7z zstd
  pdftotext qpdf gs tesseract ocrmypdf
  convert pandoc soffice
  ffmpeg ffprobe exiftool
  shellcheck ruff markdownlint-cli2
  python3
  gh gitleaks tirith rclone op himalaya
  ss dig lsof fuser
  s6-svstat
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
if [ ! -x /etc/cont-init.d/01-hermes-setup ]; then
  echo "MISSING /etc/cont-init.d/01-hermes-setup" >&2
  fail=1
fi
if ! grep -q stage2-hook /etc/cont-init.d/01-hermes-setup; then
  echo "01-hermes-setup does not exec stage2-hook" >&2
  fail=1
fi
for mcp in \
  /usr/local/lib/node_modules/@hauptsache.net/clickup-mcp \
  /usr/local/lib/node_modules/caldav-mcp; do
  if [ ! -d "$mcp" ]; then
    echo "MISSING baked MCP $mcp" >&2
    fail=1
  else
    echo "OK mcp $mcp"
  fi
done

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

# Himalaya public command is the v2.1 guard over v2.0.0 real binary.
himalaya_ver="$(himalaya --version 2>&1 || true)"
case "$himalaya_ver" in
  *v2.0.0*) echo "OK himalaya $himalaya_ver" ;;
  *) echo "BAD himalaya version: $himalaya_ver" >&2; fail=1 ;;
esac
unset HIMALAYA_WRITE_APPROVED HIMALAYA_SEND_APPROVED || true
if himalaya_out="$(himalaya message move 1 --to INBOX 2>&1)"; then
  echo "himalaya write must refuse without HIMALAYA_WRITE_APPROVED=1: $himalaya_out" >&2
  fail=1
else
  case "$himalaya_out" in
    *HIMALAYA_WRITE_APPROVED*) echo "OK himalaya write refused" ;;
    *) echo "BAD himalaya write refusal: $himalaya_out" >&2; fail=1 ;;
  esac
fi

tirith_bin="$(command -v tirith)"
tirith_file="$(file "$tirith_bin")"
echo "tirith file $tirith_file"
case "$(uname -m):$tirith_file" in
  x86_64:*x86-64*|x86_64:*x86_64*|aarch64:*ARM*|aarch64:*aarch64*)
    echo "OK tirith arch" ;;
  *)
    echo "BAD tirith architecture: $tirith_file" >&2
    fail=1 ;;
esac
tirith --version >/dev/null

# Network CLIs must ship with no baked credentials.
for p in \
  /root/.config/op /root/.op \
  /root/.config/rclone /root/.config/rclone/rclone.conf \
  /opt/data/home/.config/op /opt/data/home/.op \
  /opt/data/home/.config/rclone /opt/data/.config/rclone; do
  if [ -e "$p" ]; then
    echo "baked secret path in image: $p" >&2
    fail=1
  fi
done
rclone version >/dev/null
op --version >/dev/null
echo "OK op/rclone secret-free"

exit "$fail"
