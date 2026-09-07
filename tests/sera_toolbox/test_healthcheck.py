"""hermes-healthcheck --self-test plus marker/disk gates. No docker."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docker/sera-toolbox/hermes-healthcheck.sh"


def _run_check(home: Path, extra_path: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    if extra_path:
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    return subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


class HealthcheckSelfTest(unittest.TestCase):
    def test_self_test(self) -> None:
        r = subprocess.run(
            ["sh", str(SCRIPT), "--self-test"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_estop_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "ESTOP").write_text("", encoding="utf-8")
            r = _run_check(home)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("ESTOP set", r.stderr)

    def test_migration_in_progress_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".migration-in-progress").write_text("", encoding="utf-8")
            r = _run_check(home)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("migration in progress", r.stderr)

    def test_tight_disk_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bindir = home / "bin"
            bindir.mkdir()
            (bindir / "df").write_text(
                "#!/bin/sh\n"
                "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
                "echo 'tmpfs 100 90 1024 99% /'\n",
                encoding="utf-8",
            )
            (bindir / "df").chmod(0o755)
            r = _run_check(home, extra_path=str(bindir))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("KB free", r.stderr)


if __name__ == "__main__":
    unittest.main()
