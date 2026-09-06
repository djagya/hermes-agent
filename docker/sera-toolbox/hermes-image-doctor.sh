#!/usr/bin/env bash
# Image/runtime doctor. Default is read-only. Prune is explicit, never
# at boot: --prune-dry-run lists disposable cache; --prune deletes it
# only when HERMES_CACHE_PRUNE=1. Hugging Face / STT models are never
# deleted unless HERMES_PRUNE_MODELS=1 is also set.
set -euo pipefail

fail=0
note() { printf '%s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*" >&2; fail=1; }
ok() { printf 'OK   %s\n' "$*"; }

days="${HERMES_CACHE_PRUNE_DAYS:-30}"
home="${HERMES_HOME:-/opt/data}"
uv_cache="${UV_CACHE_DIR:-$home/cache/uv}"
hf_home="${HF_HOME:-$home/cache/huggingface}"

prune_list() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  find "$dir" -type f -atime "+$days" -print 2>/dev/null || true
}

do_prune() {
  local dry="$1"
  note "cache policy: disposable=$uv_cache durable=$hf_home age>${days}d"
  if [ "$dry" = 1 ]; then
    note "DRY-RUN disposable candidates:"
    prune_list "$uv_cache"
    if [ "${HERMES_PRUNE_MODELS:-}" = 1 ]; then
      note "DRY-RUN durable model candidates (HERMES_PRUNE_MODELS=1):"
      prune_list "$hf_home"
    else
      note "durable $hf_home listed only, not pruned (set HERMES_PRUNE_MODELS=1)"
    fi
    return 0
  fi
  if [ "${HERMES_CACHE_PRUNE:-}" != 1 ]; then
    bad "--prune refused: set HERMES_CACHE_PRUNE=1"
    return 1
  fi
  prune_list "$uv_cache" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    rm -f "$f"
    note "removed $f"
  done
  if [ "${HERMES_PRUNE_MODELS:-}" = 1 ]; then
    prune_list "$hf_home" | while IFS= read -r f; do
      [ -n "$f" ] || continue
      rm -f "$f"
      note "removed $f"
    done
  fi
}

case "${1:-}" in
  --prune-dry-run) do_prune 1; exit 0 ;;
  --prune) do_prune 0; exit "$fail" ;;
  ""|--check) ;;
  *)
    echo "usage: hermes-image-doctor [--check|--prune-dry-run|--prune]" >&2
    exit 2
    ;;
esac

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
