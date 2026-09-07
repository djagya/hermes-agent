"""stage2 required-root contract. No docker."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "docker/stage2-hook.sh"


class Stage2RequiredRoots(unittest.TestCase):
    def setUp(self) -> None:
        self.text = HOOK.read_text(encoding="utf-8")

    def test_home_not_in_recursive_remap_list(self) -> None:
        match = re.search(
            r"for sub in ([^\n]+); do",
            self.text,
        )
        self.assertIsNotNone(match)
        names = match.group(1).split()
        self.assertNotIn("home", names)
        self.assertIn("logs", names)
        self.assertIn("profiles", names)

    def test_ensure_required_root_probes_listed_dirs(self) -> None:
        self.assertIn("ensure_required_root()", self.text)
        self.assertIn("required path $target is a symlink", self.text)
        for path in (
            '"$HERMES_HOME/home"',
            '"$HERMES_HOME/cache"',
            '"$HERMES_HOME/cache/uv"',
            '"$HERMES_HOME/cache/npm"',
            '"$HERMES_HOME/cache/huggingface"',
            '"$HERMES_HOME/models"',
            '"$HERMES_HOME/state"',
            '"$HERMES_HOME/hotfixes"',
            '"$HERMES_HOME/logs"',
            '"$HERMES_HOME/logs/gateways"',
            '"$HERMES_HOME/profiles"',
        ):
            self.assertIn(path, self.text)

    def test_cachedir_tag_uv_npm_only(self) -> None:
        self.assertIn('write_cachedir_tag "$HERMES_HOME/cache/uv"', self.text)
        self.assertIn('write_cachedir_tag "$HERMES_HOME/cache/npm"', self.text)
        self.assertNotIn(
            'write_cachedir_tag "$HERMES_HOME/cache/huggingface"', self.text
        )
        self.assertNotIn('write_cachedir_tag "$HERMES_HOME/models"', self.text)
        self.assertIn(
            'ln -s "$HERMES_HOME/cache/huggingface" "$HERMES_HOME/models/huggingface"',
            self.text,
        )

    def test_cache_never_recursive_chown(self) -> None:
        self.assertNotIn('chown -R hermes:hermes "$HERMES_HOME/cache"', self.text)
        self.assertNotIn('chown_hermes_tree "$HERMES_HOME/cache"', self.text)
        self.assertNotIn('chown_hermes_tree "$HERMES_HOME/home"', self.text)
