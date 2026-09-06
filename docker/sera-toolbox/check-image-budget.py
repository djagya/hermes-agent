#!/usr/bin/env python3
"""Compare built-image measurements against docker/sera-toolbox/image-budget.json.

Null max_* means record-only for that metric. IMAGE_BYTES is required.
Optional env: LARGEST_LAYER_BYTES, COLD_HELP_MS, WARM_HELP_MS,
IDLE_RSS_KB, CACHE_WRITE_BYTES, SHUTDOWN_MS.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        print(f"ERROR: {name} is not an integer: {raw!r}", file=sys.stderr)
        raise SystemExit(2)
    return int(raw)


def _gate(budget: dict, key: str, value: int | None) -> bool:
    if value is None:
        print(f"{key}: not measured")
        return True
    print(f"{key}={value}")
    limit = budget.get(key)
    if limit is None:
        print(f"budget: record-only ({key} is null)")
        return True
    if value > int(limit):
        print(f"ERROR: {key} {value} exceeds {limit}", file=sys.stderr)
        return False
    print(f"budget: {key} {value} <= {limit}")
    return True


def main() -> int:
    budget_path = Path(os.environ.get("IMAGE_BUDGET", "docker/sera-toolbox/image-budget.json"))
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    image_bytes = _env_int("IMAGE_BYTES")
    if image_bytes is None:
        print("ERROR: IMAGE_BYTES unset or not an integer", file=sys.stderr)
        return 2
    print(f"image_bytes={image_bytes}")
    print(f"image_mib={image_bytes // 1024 // 1024}")
    print(f"accepted_digest={budget.get('accepted_digest')}")
    ok = True
    limit = budget.get("max_image_bytes")
    if limit is None:
        print("budget: record-only (max_image_bytes is null)")
    elif image_bytes > int(limit):
        print(
            f"ERROR: image {image_bytes} bytes exceeds max_image_bytes={limit}",
            file=sys.stderr,
        )
        ok = False
    else:
        print(f"budget: {image_bytes} <= {limit}")

    extras = (
        ("max_largest_layer_bytes", _env_int("LARGEST_LAYER_BYTES")),
        ("max_cold_help_ms", _env_int("COLD_HELP_MS")),
        ("max_warm_help_ms", _env_int("WARM_HELP_MS")),
        ("max_idle_rss_kb", _env_int("IDLE_RSS_KB")),
        ("max_offline_cache_write_bytes", _env_int("CACHE_WRITE_BYTES")),
        ("max_shutdown_ms", _env_int("SHUTDOWN_MS")),
    )
    for key, value in extras:
        ok = _gate(budget, key, value) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
