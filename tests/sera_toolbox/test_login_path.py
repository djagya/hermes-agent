"""Login shells must keep image PATH prefixes + umask 002."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
PROFILE = ROOT / "docker/sera-toolbox/hermes-path.sh"
SMOKE = ROOT / "docker/sera-toolbox/smoke.sh"


class LoginPathContract(unittest.TestCase):
    def test_profile_d_restores_prefixes(self) -> None:
        text = PROFILE.read_text(encoding="utf-8")
        self.assertIn("umask 002", text)
        for prefix in (
            "/opt/hermes/bin",
            "/opt/hermes/.venv/bin",
            "/command",
            "/opt/data/.local/bin",
        ):
            self.assertIn(prefix, text)
        self.assertIn("export PATH", text)

    def test_dockerfile_installs_profile_d(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn(
            "docker/sera-toolbox/hermes-path.sh /etc/profile.d/hermes-path.sh",
            text,
        )

    def test_smoke_checks_login_path(self) -> None:
        text = SMOKE.read_text(encoding="utf-8")
        self.assertIn("bash -lc", text)
        self.assertIn("/etc/profile.d/hermes-path.sh", text)
        self.assertIn("bash --noprofile --norc", text)
        self.assertIn("sourced nologin umask", text)
