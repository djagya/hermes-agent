#!/usr/bin/env bash
# Image-toolbox smoke. Run inside the built image.
set -euo pipefail

need=(
  bwrap file jq sqlite3 zip unzip 7z zstd bsdtar
  pdftotext pdffonts pdfimages qpdf gs tesseract ocrmypdf
  convert pandoc soffice
  ffmpeg ffprobe exiftool
  shellcheck ruff markdownlint-cli2
  python3
  gh gitleaks tirith rclone op himalaya
  ss dig lsof fuser
  s6-svstat
  hermes-healthcheck
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
if [ "$(command -v pdffonts)" != "/usr/local/bin/pdffonts" ]; then
  echo "pdffonts not wrapped: $(command -v pdffonts)" >&2
  fail=1
fi
if [ "$(command -v pdfimages)" != "/usr/local/bin/pdfimages" ]; then
  echo "pdfimages not wrapped: $(command -v pdfimages)" >&2
  fail=1
fi

if hermes-healthcheck --self-test; then
  echo "OK hermes-healthcheck --self-test"
else
  echo "BAD hermes-healthcheck --self-test" >&2
  fail=1
fi
if sh /opt/hermes/docker/sera-toolbox/disk-gate.sh --self-test; then
  echo "OK disk-gate --self-test"
else
  echo "BAD disk-gate --self-test" >&2
  fail=1
fi

python3 - <<'PY'
import importlib
for name in ("fitz", "weasyprint", "docx", "openpyxl", "yt_dlp", "ddgs", "fal_client", "faster_whisper"):
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
# SPDX/CycloneDX stay CI artifacts. Do not bake them under /etc/hermes.
if find /etc/hermes -maxdepth 1 \( -name '*spdx*' -o -name '*cdx*' \
     -o -name '*cyclonedx*' \) | grep -q .; then
  echo "SPDX/CycloneDX must not be baked under /etc/hermes" >&2
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
if ! python3 - <<'PY'
import hashlib
import json
import sys
from pathlib import Path

man = json.loads(Path("/etc/hermes/toolchain-manifest.json").read_text(encoding="utf-8"))
wanted = {
    "/etc/cont-init.d/01-hermes-setup",
    "/opt/hermes/docker/stage2-hook.sh",
    "/opt/hermes/docker/sera-toolbox/wrap",
    "/opt/hermes/docker/sera-toolbox/disk-gate.sh",
    "/opt/hermes/docker/sera-toolbox/check-archive-members.py",
}
recorded = man.get("init_files") or {}
fail = 0
for path in sorted(wanted):
    digest = recorded.get(path)
    if not digest:
        print(f"MISSING init_files hash for {path}", file=sys.stderr)
        fail = 1
        continue
    actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != actual:
        print(f"BAD init_files hash {path}: manifest {digest} file {actual}", file=sys.stderr)
        fail = 1
    else:
        print("OK init_files", path)
sys.exit(fail)
PY
then
  fail=1
fi
if ! grep -q 'umask 002' /opt/hermes/bin/hermes \
  || ! grep -q 'umask 002' /opt/hermes/docker/main-wrapper.sh \
  || ! grep -q 'umask 002' /opt/hermes/docker/stage2-hook.sh; then
  echo "umask 002 missing from hermes shim, main-wrapper, or stage2-hook" >&2
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

# Plan 5c: start baked MCP bins with the registry blocked. iCloud is
# caldav-mcp (no icloud-named pkg). Do not use npx pkg@ver.
start_mcp=/opt/hermes/docker/sera-toolbox/start-baked-mcp.sh
if [ ! -x "$start_mcp" ]; then
  echo "MISSING $start_mcp" >&2
  fail=1
else
  "$start_mcp" clickup "@hauptsache.net/clickup-mcp@1.8.0" || fail=1
  "$start_mcp" caldav "caldav-mcp@0.10.0" || fail=1
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
for leftover in sudo g++ make cmake; do
  if command -v "$leftover" >/dev/null 2>&1; then
    echo "$leftover must not be in runtime image: $(command -v "$leftover")" >&2
    fail=1
  fi
done
if dpkg -s python3-dev >/dev/null 2>&1; then
  echo "python3-dev must not be installed in runtime image" >&2
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

if ! grep -q 'snapshot.debian.org/archive/debian/20260907T000000Z' \
      /etc/apt/sources.list.d/debian.sources ||
   ! grep -q 'snapshot.debian.org/archive/debian-security/20260907T000000Z' \
      /etc/apt/sources.list.d/debian.sources ||
   ! grep -q 'trixie-security' /etc/apt/sources.list.d/debian.sources; then
  echo "apt sources missing Debian snapshot 20260907T000000Z main/updates/security" >&2
  fail=1
fi

case "${XDG_CACHE_HOME:-}:${UV_CACHE_DIR:-}:${HF_HOME:-}:${HERMES_MODEL_ROOT:-}" in
  /opt/data/cache:/opt/data/cache/uv:/opt/data/models/huggingface:/opt/data/models)
    echo "OK cache roots" ;;
  *)
    echo "BAD cache roots XDG_CACHE_HOME=${XDG_CACHE_HOME:-} UV_CACHE_DIR=${UV_CACHE_DIR:-} HF_HOME=${HF_HOME:-} HERMES_MODEL_ROOT=${HERMES_MODEL_ROOT:-}" >&2
    fail=1 ;;
esac

case "${HERMES_HOME:-}:${HERMES_CHILD_HOME:-}" in
  /opt/data:/opt/data/home)
    echo "OK dual-HOME env" ;;
  *)
    echo "BAD dual-HOME HERMES_HOME=${HERMES_HOME:-} HERMES_CHILD_HOME=${HERMES_CHILD_HOME:-}" >&2
    fail=1 ;;
esac

if [ ! -f /etc/profile.d/hermes-path.sh ]; then
  echo "MISSING /etc/profile.d/hermes-path.sh" >&2
  fail=1
fi
login_path="$(bash -lc 'printf %s "$PATH"')"
nologin_path="$(bash -c 'printf %s "$PATH"')"
for need in /opt/hermes/bin /opt/hermes/.venv/bin /command; do
  case ":$login_path:" in
    *":$need:"*) ;;
    *) echo "BAD login PATH missing $need: $login_path" >&2; fail=1 ;;
  esac
  case ":$nologin_path:" in
    *":$need:"*) ;;
    *) echo "BAD non-login PATH missing $need: $nologin_path" >&2; fail=1 ;;
  esac
done
login_umask="$(bash -lc 'umask')"
nologin_umask="$(bash --noprofile --norc -c '. /etc/profile.d/hermes-path.sh; umask')"
if [ "$login_umask" != "0002" ]; then
  echo "BAD login umask $login_umask (want 0002)" >&2
  fail=1
elif [ "$nologin_umask" != "0002" ]; then
  echo "BAD sourced nologin umask $nologin_umask (want 0002)" >&2
  fail=1
else
  echo "OK login PATH + umask 002"
fi

skip_home="$(mktemp -d)"
touch "$skip_home/ESTOP"
if HERMES_HOME="$skip_home" hermes-healthcheck 2>"$skip_home/err"; then
  echo "FAIL healthcheck ignored ESTOP" >&2
  fail=1
elif grep -q 'ESTOP set' "$skip_home/err"; then
  echo "OK ESTOP not-ready"
else
  echo "BAD ESTOP healthcheck: $(cat "$skip_home/err")" >&2
  fail=1
fi
rm -f "$skip_home/ESTOP"
touch "$skip_home/.migration-in-progress"
if HERMES_HOME="$skip_home" hermes-healthcheck 2>"$skip_home/err"; then
  echo "FAIL healthcheck ignored .migration-in-progress" >&2
  fail=1
elif grep -q 'migration in progress' "$skip_home/err"; then
  echo "OK migration-in-progress not-ready"
else
  echo "BAD migration-in-progress healthcheck: $(cat "$skip_home/err")" >&2
  fail=1
fi
rm -f "$skip_home/.migration-in-progress"
touch "$skip_home/.migration-skipped"
if HERMES_HOME="$skip_home" hermes-healthcheck 2>"$skip_home/err"; then
  echo "FAIL healthcheck ignored .migration-skipped" >&2
  fail=1
elif grep -q 'migration skipped' "$skip_home/err"; then
  echo "OK skip-migration not-ready"
else
  echo "BAD skip-migration healthcheck: $(cat "$skip_home/err")" >&2
  fail=1
fi
rm -f "$skip_home/.migration-skipped"
touch "$skip_home/.skills-sync-skipped"
if HERMES_HOME="$skip_home" hermes-healthcheck 2>"$skip_home/err"; then
  echo "FAIL healthcheck ignored .skills-sync-skipped" >&2
  fail=1
elif grep -q 'skills sync skipped' "$skip_home/err"; then
  echo "OK skip-skills not-ready"
else
  echo "BAD skip-skills healthcheck: $(cat "$skip_home/err")" >&2
  fail=1
fi
rm -rf "$skip_home"

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

# Downloaded CLIs: file(1) + --version, not --version alone.
for name in tirith gh gitleaks op; do
  bin="$(command -v "$name")"
  desc="$(file "$bin")"
  echo "$name file $desc"
  case "$(uname -m):$desc" in
    x86_64:*x86-64*|x86_64:*x86_64*|aarch64:*ARM*|aarch64:*aarch64*)
      echo "OK $name arch" ;;
    *)
      echo "BAD $name architecture: $desc" >&2
      fail=1 ;;
  esac
  "$name" --version >/dev/null
done

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
