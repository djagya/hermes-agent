"""cont-init /run/service and supervise chown are fail-closed."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECONCILE = ROOT / "docker/cont-init.d/02-reconcile-profiles"
SUPERVISE = ROOT / "docker/cont-init.d/015-supervise-perms"


class ContInitFailClosed(unittest.TestCase):
    def test_reconcile_profiles_fail_closed(self) -> None:
        text = RECONCILE.read_text(encoding="utf-8")
        self.assertNotIn("chown hermes:hermes /run/service 2>/dev/null || true", text)
        self.assertIn('ERROR: chown /run/service failed', text)
        self.assertIn("/run/service/.stage2-write-probe", text)
        self.assertIn("ERROR: /run/service/.s6-svscan missing", text)

    def test_supervise_perms_fail_closed(self) -> None:
        text = SUPERVISE.read_text(encoding="utf-8")
        self.assertNotIn("exit 0", text)
        self.assertIn("ERROR: $SVC_ROOT not present", text)
        self.assertIn("ERROR: could not chown $svc/supervise", text)
        self.assertIn("ERROR: could not chown $svc/event", text)
