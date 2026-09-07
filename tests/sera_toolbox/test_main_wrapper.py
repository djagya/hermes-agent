"""main-wrapper: no-arg prints help; mcp cannot hit the developer CLI."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "docker/main-wrapper.sh"
SHIM = ROOT / "docker/mcp-shim.sh"


class MainWrapperContract(unittest.TestCase):
    def test_no_arg_is_help(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("[ $# -eq 0 ]", text)
        self.assertIn("drop hermes --help", text)

    def test_mcp_routes_to_hermes(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn('[ "$1" = "mcp" ]', text)
        self.assertIn("drop hermes \"$@\"", text)
        shim = SHIM.read_text(encoding="utf-8")
        self.assertIn("exec /opt/hermes/bin/hermes mcp", shim)
