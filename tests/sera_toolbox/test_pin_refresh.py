"""Unit tests for pin-refresh PR body. No network."""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "pin_refresh", ROOT / "docker/sera-toolbox/pin-refresh.py"
)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class ImageBudgetFileTests(unittest.TestCase):
    def test_budget_file_has_b3_accepted_tuple(self) -> None:
        budget = json.loads(
            (ROOT / "docker/sera-toolbox/image-budget.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(budget["schema"], 1)
        self.assertEqual(
            budget["accepted_digest"],
            "sha256:d63fc55532a760f7981baa6de696d44ed8dd778c60f0aa707f703c3f7ca24c30",
        )
        self.assertEqual(
            budget["accepted_git_sha"],
            "e3391400e6c04dc30c0bebe2c797f0f07ca0ef09",
        )
        self.assertEqual(budget["measured_image_bytes"], 4494315525)
        self.assertGreater(budget["max_image_bytes"], budget["measured_image_bytes"])
        self.assertEqual(budget["max_fixable_critical"], 0)
        self.assertEqual(budget["max_fixable_high"], 0)


class PinRefreshParseTests(unittest.TestCase):
    def test_parse_dockerfile_has_toolbox_pins(self) -> None:
        pins = mod.parse_dockerfile()
        self.assertTrue(pins["himalaya"].startswith("b1f6dece"))
        self.assertEqual(pins["weasyprint"], "69.0")
        self.assertEqual(pins["python_docx"], "1.2.0")
        self.assertEqual(pins["openpyxl"], "3.1.5")
        self.assertEqual(pins["yt_dlp"], "2026.08.19")
        self.assertEqual(pins["clickup"], "1.8.0")
        self.assertEqual(pins["caldav"], "0.10.0")


class PinRefreshReportTests(unittest.TestCase):
    def test_render_report_includes_budget_not_later_placeholder(self) -> None:
        budget = {
            "accepted_digest": "sha256:deadbeef",
            "accepted_git_sha": "abc123",
            "measured_image_bytes": 100,
            "max_image_bytes": 150,
            "max_largest_layer_bytes": 50,
            "max_fixable_critical": 0,
            "max_fixable_high": 0,
        }
        body = mod.render_report(
            budget,
            [("gh", "2.0.0", "2.1.0", "https://example.invalid")],
            ["gh 2.0.0 -> 2.1.0"],
            [],
        )
        self.assertNotIn("CI on this branch must fill", body)
        self.assertIn("sha256:deadbeef", body)
        self.assertIn("slack `50`", body)
        self.assertIn("max_fixable_critical: `0`", body)
        self.assertIn("hermes-agent-sbom", body)
        self.assertIn("| gh | `2.0.0` | `2.1.0` | bump", body)
        self.assertIn("gh 2.0.0 -> 2.1.0", body)

    def test_render_report_lookup_error_section(self) -> None:
        body = mod.render_report({}, [], [], ["debian: timeout"])
        self.assertIn("## Lookup errors", body)
        self.assertIn("debian: timeout", body)
        self.assertIn("(unset)", body)

    def test_apply_never_rewrites_report_only_pins(self) -> None:
        src = (ROOT / "docker/sera-toolbox/pin-refresh.py").read_text(
            encoding="utf-8"
        )
        marker = "# Report-only. Himalaya is a pinned git rev"
        self.assertIn(marker, src)
        tail = src.split(marker, 1)[1]
        self.assertNotIn("args.apply", tail)
        self.assertNotIn("DOCKERFILE.write_text", tail)
        self.assertNotIn("applied.append", tail)
        for name in (
            "himalaya",
            "weasyprint",
            "python-docx",
            "openpyxl",
            "yt-dlp",
            "clickup-mcp",
            "caldav-mcp",
        ):
            self.assertIn(f'row("{name}"', tail)


if __name__ == "__main__":
    unittest.main()
