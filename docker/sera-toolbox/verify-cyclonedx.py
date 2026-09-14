#!/usr/bin/env python3
"""Fail unless a named CycloneDX 1.x JSON SBOM is present and non-empty."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: verify-cyclonedx.py FILE.cdx.json", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"ERROR: missing CycloneDX file {path}", file=sys.stderr)
        return 1
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: CycloneDX is not JSON: {exc}", file=sys.stderr)
        return 1
    bom = str(doc.get("bomFormat") or "")
    version = str(doc.get("specVersion") or "")
    components = doc.get("components")
    if bom != "CycloneDX":
        print(f"ERROR: bomFormat={bom!r}", file=sys.stderr)
        return 1
    if not version:
        print("ERROR: missing specVersion", file=sys.stderr)
        return 1
    if not isinstance(components, list) or len(components) < 1:
        print("ERROR: CycloneDX components empty", file=sys.stderr)
        return 1
    print(f"OK CycloneDX {version} components={len(components)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
