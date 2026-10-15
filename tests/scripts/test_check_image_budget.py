"""docker/sera-toolbox/check-image-budget.py size verdicts. No docker."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docker/sera-toolbox/check-image-budget.py"


def _check(tmp_path: Path, image_bytes: int, method: str | None) -> int:
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"max_image_bytes": 1_000_000, "du_margin_ratio": 0.01}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("IMAGE_")}
    env.update(IMAGE_BUDGET=str(budget), IMAGE_BYTES=str(image_bytes))
    if method:
        env["IMAGE_BYTES_METHOD"] = method
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, timeout=30, check=False
    ).returncode


def test_du_measurement_keeps_a_margin_below_the_ci_cap(tmp_path):
    # du on the box reads about 0.6% below CI's .Size: within 1% of the cap fails.
    assert _check(tmp_path, 995_000, "du") == 1
    assert _check(tmp_path, 990_000, "du") == 0
    assert _check(tmp_path, 995_000, None) == 0
    assert _check(tmp_path, 1_000_001, "inspect") == 1
    assert _check(tmp_path, 1, "bogus") == 2
