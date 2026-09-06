#!/usr/bin/env bash
# Bake /etc/hermes/toolchain-manifest.json (name, version, path, sha256).
# No secrets.
set -euo pipefail

mkdir -p /etc/hermes
python3 - <<'PY'
import hashlib
import json
import shutil
import subprocess
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
    digest = sha256(exe)
    return {"name": cmd, "path": exe, "version": version, "sha256": digest}

def sha256(path):
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()

tools = [
    "bwrap", "file", "sqlite3", "jq", "pdftotext", "qpdf", "gs",
    "tesseract", "convert", "pandoc", "soffice", "ffmpeg", "exiftool",
    "shellcheck", "ruff", "gh", "gitleaks", "tirith", "rclone", "op",
    "markdownlint-cli2", "hermes", "weasyprint", "sera-pymupdf",
]
# Plan 5c: record both the cont-init shim and the hook it execs. A
# service restart does not rerun cont-init; hashes prove the baked
# execution path, not just the source copy under /opt/hermes.
init_files = {
    "/etc/cont-init.d/01-hermes-setup": sha256("/etc/cont-init.d/01-hermes-setup"),
    "/opt/hermes/docker/stage2-hook.sh": sha256("/opt/hermes/docker/stage2-hook.sh"),
}
payload = {
    "schema": 1,
    "tools": [ver(t) for t in tools],
    "init_files": init_files,
}
path = Path("/etc/hermes/toolchain-manifest.json")
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o444)
print("wrote", path)
PY
