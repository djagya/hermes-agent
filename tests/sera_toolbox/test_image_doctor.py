"""doctor --full must run golden-smoke when fixtures exist."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / "docker/sera-toolbox/hermes-image-doctor.sh"


class ImageDoctorFull(unittest.TestCase):
    def test_full_execs_golden_smoke(self) -> None:
        text = DOCTOR.read_text(encoding="utf-8")
        self.assertIn('SERA_GOLDEN_FIXTURES="$fix" "$golden"', text)
        self.assertNotIn(
            "golden-smoke is a separate CI entrypoint",
            text,
        )

    def test_prune_dry_run_skips_models_unless_flag(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            uv = home / "cache" / "uv"
            npm = home / "cache" / "npm"
            hf = home / "models" / "huggingface"
            uv.mkdir(parents=True)
            npm.mkdir(parents=True)
            hf.mkdir(parents=True)
            (uv / "old.bin").write_bytes(b"u")
            (hf / "model.bin").write_bytes(b"m")
            os.utime(uv / "old.bin", (0, 0))
            os.utime(hf / "model.bin", (0, 0))
            env = os.environ.copy()
            env.update(
                {
                    "HERMES_HOME": str(home),
                    "UV_CACHE_DIR": str(uv),
                    "NPM_CONFIG_CACHE": str(npm),
                    "HF_HOME": str(hf),
                    "HERMES_MODEL_ROOT": str(home / "models"),
                    "HERMES_CACHE_PRUNE_DAYS": "0",
                }
            )
            dry = subprocess.run(
                [str(DOCTOR), "--prune-dry-run"],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertIn("DRY-RUN disposable candidates:", dry.stdout)
            self.assertIn(str(uv / "old.bin"), dry.stdout)
            self.assertNotIn(str(hf / "model.bin"), dry.stdout)
            self.assertIn("listed only, not pruned", dry.stdout)
            env["HERMES_CACHE_PRUNE"] = "1"
            subprocess.run(
                [str(DOCTOR), "--prune"],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertFalse((uv / "old.bin").exists())
            self.assertTrue((hf / "model.bin").exists())
