"""Tag stage2 keeps disk-gate + migrate-fail. No docker."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "docker/stage2-hook.sh"


class Stage2MigrateFail(unittest.TestCase):
    def setUp(self) -> None:
        self.text = HOOK.read_text(encoding="utf-8")

    def test_migrate_fail_exits(self) -> None:
        self.assertIn(
            'echo "[stage2] ERROR: docker_config_migrate.py failed" >&2; exit 1;',
            self.text,
        )
        self.assertNotIn("docker_config_migrate.py failed; continuing", self.text)

    def test_disk_gate_runs_after_mkdir(self) -> None:
        self.assertIn("docker/sera-toolbox/disk-gate.sh", self.text)
        mkdir_at = self.text.find('mkdir -p "$HERMES_HOME"')
        gate_at = self.text.find("disk-gate.sh")
        self.assertGreater(mkdir_at, 0)
        self.assertGreater(gate_at, mkdir_at)


if __name__ == "__main__":
    unittest.main()
