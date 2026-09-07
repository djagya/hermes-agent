"""Baked /etc/hermes managed policy must refuse hermes config set."""

from __future__ import annotations

import subprocess


def test_config_set_cannot_weaken_write_approval(built_image: str) -> None:
    """Plan 5c: hermes config set cannot flip managed write_approval."""
    r = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-e", "HERMES_HOME=/tmp/hermes-home",
            "-e", "HOME=/tmp/hermes-home",
            "--entrypoint", "/opt/hermes/.venv/bin/hermes",
            built_image,
            "config", "set", "memory.write_approval", "true",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = (r.stdout + r.stderr).lower()
    assert r.returncode != 0, (
        f"managed key was writable: stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    assert "managed" in combined
