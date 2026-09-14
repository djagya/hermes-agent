"""Office VBA/AutoOpen fixture + wrap MacroSecurityLevel 3, no --safe-mode."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRAP = ROOT / "docker/sera-toolbox/wrap"
XCU = ROOT / "docker/sera-toolbox/libreoffice/registrymodifications.xcu"
MACRO = ROOT / "docker/sera-toolbox/adversarial/office-macro.sh"
WORKFLOW = ROOT / ".github/workflows/fork-release-image.yml"


class OfficeMacroHardening(unittest.TestCase):
    def test_wrap_uses_macro_security_not_safe_mode(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        self.assertIn("MacroSecurityLevel 3", text)
        self.assertIn("registrymodifications.xcu", text)
        self.assertIn("Do not pass --safe-mode", text)
        self.assertNotIn("--safe-mode", text.replace("Do not pass --safe-mode", ""))

    def test_xcu_level_three(self) -> None:
        text = XCU.read_text(encoding="utf-8")
        self.assertIn('oor:name="MacroSecurityLevel"', text)
        self.assertIn("<value>3</value>", text)

    def test_adversarial_script_is_vba_shaped(self) -> None:
        text = MACRO.read_text(encoding="utf-8")
        self.assertIn("vbaProject.bin", text)
        self.assertIn("Auto_Open", text)
        self.assertIn("eventDocOpen", text)
        self.assertIn("127.0.0.1", text)

    def test_ci_runs_office_macro(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("adversarial/office-macro.sh", text)
