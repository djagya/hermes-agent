"""GNU tar stays unwrapped. bsdtar/7z are the sandboxed extractors."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "docker/sera-toolbox/install-wrappers.sh"
WRAP = ROOT / "docker/sera-toolbox/wrap"


class GnuTarUnwrapped(unittest.TestCase):
    def test_install_wrappers_omits_gnu_tar(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        match = re.search(r"tools=\((.*?)\)", text, flags=re.S)
        self.assertIsNotNone(match)
        tools = match.group(1)
        names = set(re.findall(r"[A-Za-z0-9._+-]+", tools))
        self.assertIn("bsdtar", names)
        self.assertIn("7z", names)
        self.assertNotIn("tar", names)

    def test_wrap_checks_bsdtar_args(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        self.assertIn("unzip|7z|7za|bsdtar)", text)
        self.assertIn("check-archive-members.py", text)

    def test_wrap_archive_case_omits_gnu_tar(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        self.assertIn("unzip|7z|7za|bsdtar)", text)
        self.assertNotRegex(text, r"(?<![A-Za-z0-9_])tar\|")
        self.assertNotRegex(text, r"\|tar(?![A-Za-z0-9_])")
