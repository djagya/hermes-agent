#!/usr/bin/env python3
"""Compare a built image size against docker/sera-toolbox/image-budget.json.

Null max_image_bytes means record-only (first accepted B digest fills it).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    budget_path = Path(os.environ.get("IMAGE_BUDGET", "docker/sera-toolbox/image-budget.json"))
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    raw = os.environ.get("IMAGE_BYTES", "").strip()
    if not raw.isdigit():
        print("ERROR: IMAGE_BYTES unset or not an integer", file=sys.stderr)
        return 2
    image_bytes = int(raw)
    print(f"image_bytes={image_bytes}")
    print(f"image_mib={image_bytes // 1024 // 1024}")
    print(f"accepted_digest={budget.get('accepted_digest')}")
    limit = budget.get("max_image_bytes")
    if limit is None:
        print("budget: record-only (max_image_bytes is null)")
        return 0
    if image_bytes > int(limit):
        print(
            f"ERROR: image {image_bytes} bytes exceeds max_image_bytes={limit}",
            file=sys.stderr,
        )
        return 1
    print(f"budget: {image_bytes} <= {limit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
