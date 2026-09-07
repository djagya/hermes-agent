"""Baked /etc/hermes managed seed fills missing leaves; live set wins."""

from __future__ import annotations

import subprocess


def _hermes(built_image: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-e", "HERMES_HOME=/tmp/hermes-home",
            "-e", "HOME=/tmp/hermes-home",
            "--entrypoint", "/bin/bash",
            built_image,
            "-lc",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_managed_seed_fills_missing_write_approval(built_image: str) -> None:
    r = _hermes(
        built_image,
        "/opt/hermes/.venv/bin/hermes config get memory.write_approval",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "false"


def test_config_set_overrides_managed_write_approval(built_image: str) -> None:
    r = _hermes(
        built_image,
        "set -euo pipefail; "
        "/opt/hermes/.venv/bin/hermes config set memory.write_approval true; "
        "/opt/hermes/.venv/bin/hermes config get memory.write_approval; "
        "/opt/hermes/.venv/bin/hermes config unset memory.write_approval; "
        "/opt/hermes/.venv/bin/hermes config get memory.write_approval",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip() in {"true", "false"}]
    assert lines == ["true", "false"], r.stdout + r.stderr


def test_config_set_can_disable_skills_write_approval(built_image: str) -> None:
    r = _hermes(
        built_image,
        "set -euo pipefail; "
        "/opt/hermes/.venv/bin/hermes config set skills.write_approval false; "
        "/opt/hermes/.venv/bin/hermes config get skills.write_approval",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == "false"
