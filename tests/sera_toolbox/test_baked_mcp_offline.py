"""Baked MCP hooks start with the registry blocked (plan 5c)."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "docker/sera-toolbox/start-baked-mcp.sh"
SMOKE = ROOT / "docker/sera-toolbox/smoke.sh"
DOCKERFILE = ROOT / "Dockerfile"


class BakedMcpOffline(unittest.TestCase):
    def test_helper_blocks_registry_and_uses_npx_no_install(self) -> None:
        text = HELPER.read_text(encoding="utf-8")
        self.assertIn('NPM_CONFIG_REGISTRY="http://127.0.0.1:9"', text)
        self.assertIn("npx --no-install", text)
        self.assertIn("timeout", text)
        self.assertNotIn("npx -y", text)
        self.assertNotIn("npx --yes", text)

    def test_smoke_starts_clickup_and_caldav(self) -> None:
        text = SMOKE.read_text(encoding="utf-8")
        self.assertIn("start-baked-mcp.sh", text)
        self.assertIn("@hauptsache.net/clickup-mcp@1.8.0", text)
        self.assertIn("caldav-mcp@0.10.0", text)

    def test_dockerfile_copies_helper(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn(
            "docker/sera-toolbox/start-baked-mcp.sh",
            text,
        )

    def test_toolchain_manifest_hashes_helper(self) -> None:
        text = (
            ROOT / "docker/sera-toolbox/write-toolchain-manifest.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("start-baked-mcp.sh", text)


if __name__ == "__main__":
    unittest.main()
