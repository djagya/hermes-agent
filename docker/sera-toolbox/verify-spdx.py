#!/usr/bin/env python3
"""Fail unless a named SPDX 2.x JSON SBOM is present and non-empty."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: verify-spdx.py FILE.spdx.json", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"ERROR: missing SPDX file {path}", file=sys.stderr)
        return 1
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: SPDX is not JSON: {exc}", file=sys.stderr)
        return 1
    version = str(doc.get("spdxVersion") or "")
    spdx_id = str(doc.get("SPDXID") or "")
    name = str(doc.get("name") or "")
    packages = doc.get("packages")
    if not version.startswith("SPDX-"):
        print(f"ERROR: spdxVersion={version!r}", file=sys.stderr)
        return 1
    if not spdx_id:
        print("ERROR: missing SPDXID", file=sys.stderr)
        return 1
    if not name:
        print("ERROR: missing SPDX name", file=sys.stderr)
        return 1
    if not isinstance(packages, list) or len(packages) < 1:
        print("ERROR: SPDX packages empty", file=sys.stderr)
        return 1
    print(f"OK SPDX {version} id={spdx_id} name={name} packages={len(packages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
