"""disk-gate.sh --self-test. No docker."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docker/sera-toolbox/disk-gate.sh"


class DiskGateSelfTest(unittest.TestCase):
    def test_self_test(self) -> None:
        r = subprocess.run(
            ["sh", str(SCRIPT), "--self-test"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
