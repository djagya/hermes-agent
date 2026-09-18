"""wrap must use workload-specific timeout/AS profiles, not one value."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRAP = ROOT / "docker/sera-toolbox/wrap"


def _defaults() -> dict[str, tuple[int, int]]:
    """tool -> (timeout_s, as_bytes) from the wrap case table."""
    text = WRAP.read_text(encoding="utf-8")
    start = text.index("# Workload profiles")
    end = text.index("nproc_lim=", start)
    block = text[start:end]
    out: dict[str, tuple[int, int]] = {}
    tools: list[str] = []
    timeout = None
    as_bytes = None
    for raw in block.splitlines():
        m = re.match(r"\s+([a-z0-9_|*-]+)\)$", raw)
        if m:
            tools = [t for t in m.group(1).split("|") if t != "*"]
            timeout = None
            as_bytes = None
            continue
        tm = re.search(r'SERA_SANDBOX_TIMEOUT:-(\d+)', raw)
        if tm:
            timeout = int(tm.group(1))
        am = re.search(r'SERA_SANDBOX_AS_BYTES:-(\d+)', raw)
        if am:
            as_bytes = int(am.group(1))
        if tools and timeout is not None and as_bytes is not None:
            for tool in tools:
                out[tool] = (timeout, as_bytes)
            tools = []
            timeout = None
            as_bytes = None
    return out


class WrapWorkloadProfiles(unittest.TestCase):
    def test_profiles_differ_by_class(self) -> None:
        d = _defaults()
        self.assertEqual(d["pdfinfo"], (30, 536870912))
        self.assertEqual(d["pdftotext"], (120, 1073741824))
        self.assertEqual(d["pdffonts"], (120, 1073741824))
        self.assertEqual(d["pdfimages"], (120, 1073741824))
        self.assertEqual(d["convert"], (120, 1073741824))
        self.assertEqual(d["magick"], (120, 1073741824))
        self.assertEqual(d["mogrify"], (120, 1073741824))
        self.assertEqual(d["heif-convert"], (120, 1073741824))
        self.assertEqual(d["ocrmypdf"], (180, 2147483648))
        self.assertEqual(d["soffice"], (180, 2147483648))
        self.assertEqual(d["ffmpeg"], (120, 1073741824))
        self.assertEqual(d["unzip"], (60, 1073741824))
        timeouts = {t for t, _ in d.values()}
        self.assertGreaterEqual(len(timeouts), 3, d)

    def test_caller_env_still_wins(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        self.assertIn("Caller SERA_SANDBOX_TIMEOUT / _AS_BYTES win", text)
        self.assertIn('timeout_s="${SERA_SANDBOX_TIMEOUT:-', text)


if __name__ == "__main__":
    unittest.main()
