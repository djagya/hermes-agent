"""Standalone tests for cache-root re-pin. No pytest."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import hermes_constants  # noqa: E402


class CacheRootPinTests(unittest.TestCase):
    def test_copies_image_env_and_aligns_hf_siblings(self) -> None:
        original = hermes_constants.is_container
        hermes_constants.is_container = lambda: True  # type: ignore[method-assign]
        self.addCleanup(setattr, hermes_constants, "is_container", original)
        with tempfile.TemporaryDirectory() as raw:
            hermes_home = Path(raw) / "data"
            child = hermes_home / "home"
            child.mkdir(parents=True)
            os.environ["HERMES_HOME"] = str(hermes_home)
            os.environ["HERMES_CHILD_HOME"] = str(child)
            os.environ["XDG_CACHE_HOME"] = "/opt/data/cache"
            os.environ["HF_HOME"] = "/opt/data/models/huggingface"
            os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/hermes/.playwright"
            os.environ.pop("TRANSFORMERS_CACHE", None)
            os.environ.pop("HUGGINGFACE_HUB_CACHE", None)
            env = {"HOME": str(hermes_home), "HERMES_HOME": str(hermes_home)}
            hermes_constants.apply_subprocess_home_env(env)
            self.assertEqual(env["HOME"], str(child))
            self.assertEqual(env["XDG_CACHE_HOME"], "/opt/data/cache")
            self.assertEqual(env["HF_HOME"], "/opt/data/models/huggingface")
            self.assertEqual(env["TRANSFORMERS_CACHE"], "/opt/data/models/huggingface")
            self.assertEqual(env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"], "1")
            self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/opt/hermes/.playwright")

    def test_does_not_invent_without_image_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            hermes_home = Path(raw) / "data"
            child = hermes_home / "home"
            child.mkdir(parents=True)
            os.environ["HERMES_HOME"] = str(hermes_home)
            os.environ["HERMES_CHILD_HOME"] = str(child)
            for key in (
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "UV_CACHE_DIR",
                "NPM_CONFIG_CACHE",
                "HF_HOME",
                "TRANSFORMERS_CACHE",
                "HUGGINGFACE_HUB_CACHE",
                "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
                "PLAYWRIGHT_BROWSERS_PATH",
            ):
                os.environ.pop(key, None)
            env = {"HOME": str(hermes_home), "HERMES_HOME": str(hermes_home)}
            hermes_constants.apply_subprocess_home_env(env)
            self.assertNotIn("XDG_CACHE_HOME", env)
            self.assertNotIn("HF_HOME", env)


if __name__ == "__main__":
    unittest.main()
