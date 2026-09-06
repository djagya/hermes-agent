#!/usr/bin/env bash
# Read-only image/runtime doctor. No secrets, no mutations.
set -euo pipefail

fail=0
note() { printf '%s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*" >&2; fail=1; }
ok() { printf 'OK   %s\n' "$*"; }

if [ -f /etc/hermes/image-provenance.json ]; then
  ok "provenance marker present"
else
  bad "missing /etc/hermes/image-provenance.json"
fi
if [ -f /etc/hermes/toolchain-manifest.json ]; then
  ok "toolchain manifest present"
else
  bad "missing /etc/hermes/toolchain-manifest.json"
fi

if [ -d /opt/hermes ] && [ ! -w /opt/hermes ]; then
  ok "/opt/hermes not writable by $(id -un)"
else
  # root during image build / doctor-as-root is expected.
  if [ "$(id -u)" = 0 ]; then
    ok "/opt/hermes writable by root (expected)"
  else
    bad "/opt/hermes writable by non-root"
  fi
fi

home="${HERMES_HOME:-/opt/data}"
if [ -d "$home" ]; then
  if [ -w "$home" ]; then
    ok "$home writable"
  else
    bad "$home not writable"
  fi
  avail_kb="$(df -Pk "$home" 2>/dev/null | awk 'NR==2 {print $4}')"
  inodes="$(df -Pi "$home" 2>/dev/null | awk 'NR==2 {print $4}')"
  note "disk ${avail_kb:-?}KB free, ${inodes:-?} inodes free on $home"
  for cache_dir in \
      "${XDG_CACHE_HOME:-$home/cache}" \
      "${UV_CACHE_DIR:-$home/cache/uv}" \
      "${HF_HOME:-$home/cache/huggingface}"; do
    if [ -d "$cache_dir" ]; then
      cache_kb="$(du -sk "$cache_dir" 2>/dev/null | awk '{print $1}')"
      note "cache ${cache_dir} ${cache_kb:-?}KB (no boot prune)"
    else
      note "cache ${cache_dir} absent (stage2 seeds it)"
    fi
  done
else
  bad "$home missing"
fi

if ! command -v hermes >/dev/null 2>&1; then
  bad "hermes not on PATH"
else
  ok "hermes -> $(command -v hermes)"
fi
if ! command -v s6-svstat >/dev/null 2>&1; then
  note "WARN s6-svstat not on PATH (ok outside supervised container)"
else
  ok "s6-svstat -> $(command -v s6-svstat)"
fi

python3 - <<'PY' || bad "required python imports failed"
import importlib
for name in ("fitz", "weasyprint", "yt_dlp", "ddgs", "fal_client", "faster_whisper"):
    importlib.import_module(name)
    print("OK   py", name)
PY

exit "$fail"
