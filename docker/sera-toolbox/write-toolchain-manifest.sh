#!/usr/bin/env bash
# Bake /etc/hermes/toolchain-manifest.json (name, version, path). No secrets.
set -euo pipefail

mkdir -p /etc/hermes
python3 - <<'PY'
import json, shutil, subprocess
from pathlib import Path

def ver(cmd):
    exe = shutil.which(cmd)
    if not exe:
        return {"name": cmd, "path": None, "version": None}
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        text = (p.stdout or p.stderr or "").strip().splitlines()
        version = text[0] if text else ""
    except Exception as exc:  # noqa: BLE001
        version = f"error:{exc}"
    return {"name": cmd, "path": exe, "version": version}

tools = [
    "bwrap", "file", "sqlite3", "jq", "pdftotext", "qpdf", "gs",
    "tesseract", "convert", "pandoc", "soffice", "ffmpeg", "exiftool",
    "shellcheck", "ruff", "gh", "gitleaks", "tirith", "rclone",
    "markdownlint-cli2", "hermes",
]
payload = {
    "schema": 1,
    "tools": [ver(t) for t in tools],
}
path = Path("/etc/hermes/toolchain-manifest.json")
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o444)
print("wrote", path)
PY
