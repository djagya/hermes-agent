#!/usr/bin/env bash
# Sanitized image identity for support / doctor. Never print env or secrets.
set -euo pipefail

json=0
if [ "${1:-}" = "--json" ]; then
  json=1
fi

read_json_file() {
  if [ -f "$1" ]; then
    cat "$1"
  else
    echo "{}"
  fi
}

prov="$(read_json_file /etc/hermes/image-provenance.json)"
tools="$(read_json_file /etc/hermes/toolchain-manifest.json)"

python_ver="$(python3 -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo unknown)"
node_ver="$(node --version 2>/dev/null || echo unknown)"
sqlite_ver="$(sqlite3 -version 2>/dev/null | awk '{print $1}' || echo unknown)"
hermes_ver="$(hermes --version 2>/dev/null | head -1 || echo unknown)"
arch="$(uname -m)"
uid="$(id -u)"
gid="$(id -g)"

if [ "$json" -eq 1 ]; then
  PROV="$prov" TOOLS="$tools" \
  PY="$python_ver" NODE="$node_ver" SQLITE="$sqlite_ver" HERMES="$hermes_ver" \
  ARCH="$arch" UID_N="$uid" GID_N="$gid" \
  python3 - <<'PY'
import json, os
out = {
    "provenance": json.loads(os.environ["PROV"] or "{}"),
    "toolchain": json.loads(os.environ["TOOLS"] or "{}"),
    "versions": {
        "hermes": os.environ["HERMES"],
        "python": os.environ["PY"],
        "node": os.environ["NODE"],
        "sqlite": os.environ["SQLITE"],
    },
    "runtime": {
        "arch": os.environ["ARCH"],
        "uid": int(os.environ["UID_N"]),
        "gid": int(os.environ["GID_N"]),
        "path": os.environ.get("PATH", ""),
        "home": os.environ.get("HOME", ""),
        "hermes_home": os.environ.get("HERMES_HOME", ""),
        "xdg_config_home": os.environ.get("XDG_CONFIG_HOME", ""),
        "write_safe_root": os.environ.get("HERMES_WRITE_SAFE_ROOT", ""),
    },
}
print(json.dumps(out, indent=2, sort_keys=True))
PY
  exit 0
fi

echo "=== provenance ==="
echo "$prov"
echo
echo "=== versions ==="
echo "hermes  $hermes_ver"
echo "python  $python_ver"
echo "node    $node_ver"
echo "sqlite  $sqlite_ver"
echo "arch    $arch"
echo "uid/gid ${uid}:${gid}"
echo
echo "=== toolbox ==="
for c in bwrap file sqlite3 jq pdftotext qpdf gs tesseract convert pandoc soffice \
         ffmpeg exiftool shellcheck ruff gh gitleaks tirith rclone op yt-dlp \
         markdownlint-cli2; do
  if command -v "$c" >/dev/null 2>&1; then
    printf '%-20s %s\n' "$c" "$(command -v "$c")"
  else
    printf '%-20s MISSING\n' "$c"
  fi
done
echo
echo "=== wrappers ==="
if [ -x /usr/libexec/sera-toolbox/pdftotext ]; then
  echo "libexec pdftotext present"
  echo "PATH pdftotext=$(command -v pdftotext)"
else
  echo "libexec parsers not installed"
fi
