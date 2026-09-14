"""check-archive-members.py zip-slip / tar-slip. No docker."""
from __future__ import annotations

import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docker/sera-toolbox/check-archive-members.py"
WORKFLOW = ROOT / ".github/workflows/fork-release-image.yml"
_spec = importlib.util.spec_from_file_location("check_archive_members", SCRIPT)
assert _spec and _spec.loader
cam = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cam)


class ArchiveMembers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_zip_slip(self) -> None:
        p = self.dir / "slip.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("../escape.txt", "evil")
        self.assertEqual(cam.check_path(p), 2)

    def test_zip_ok(self) -> None:
        p = self.dir / "ok.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("safe.txt", "ok")
        self.assertEqual(cam.check_path(p), 0)

    def test_tar_slip(self) -> None:
        p = self.dir / "slip.tar"
        payload = io.BytesIO(b"evil")
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = 4
        with tarfile.open(p, "w") as t:
            t.addfile(info, payload)
        self.assertEqual(cam.check_path(p), 2)

    def test_tar_ok(self) -> None:
        p = self.dir / "ok.tar"
        payload = io.BytesIO(b"ok")
        info = tarfile.TarInfo(name="safe.txt")
        info.size = 2
        with tarfile.open(p, "w") as t:
            t.addfile(info, payload)
        self.assertEqual(cam.check_path(p), 0)

    def test_tar_ratio(self) -> None:
        p = self.dir / "bomb.tar.gz"
        blob = b"\x00" * (2 * 1024 * 1024)
        info = tarfile.TarInfo(name="zeros.bin")
        info.size = len(blob)
        with tarfile.open(p, "w:gz") as t:
            t.addfile(info, io.BytesIO(blob))
        self.assertEqual(cam.check_path(p), 3)

    def test_parse_7z_slt_skips_archive_path(self) -> None:
        listing = """\
Path = bomb.7z
Size = 200
Packed Size = 200
Path = zeros.bin
Size = 2097152
Packed Size = 200
"""
        raw, packed, members, names = cam.parse_7z_slt(listing, skip_name="bomb.7z")
        self.assertEqual(members, 1)
        self.assertEqual(names, ["zeros.bin"])
        self.assertEqual(raw, 2097152)
        self.assertEqual(packed, 200)

    def test_7z_ratio_via_origin_override(self) -> None:
        fake = self.dir / "fake7z"
        fake.write_text(
            "#!/bin/sh\ncat <<'EOF'\n"
            "Path = bomb.7z\nSize = 200\nPacked Size = 200\n"
            "Path = zeros.bin\nSize = 2097152\nPacked Size = 200\n"
            "EOF\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        p = self.dir / "bomb.7z"
        p.write_bytes(cam.SEVEN_Z_MAGIC + b"\x00\x00")
        old = os.environ.get("SERA_SANDBOX_7Z")
        os.environ["SERA_SANDBOX_7Z"] = str(fake)
        try:
            self.assertEqual(cam.check_path(p), 3)
        finally:
            if old is None:
                os.environ.pop("SERA_SANDBOX_7Z", None)
            else:
                os.environ["SERA_SANDBOX_7Z"] = old

    def test_ci_runs_high_ratio_7z(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("adversarial/high-ratio-7z.sh", text)
        self.assertIn("adversarial/high-ratio-zstd.sh", text)

    def test_parse_bsdtar_tv(self) -> None:
        listing = (
            "-rw-r--r--  0 user group  2097152 Jan  1  2026 zeros.bin\n"
            "-rw-r--r--  0 user group        4 Jan  1  2026 ok.txt\n"
        )
        raw, members, names = cam.parse_bsdtar_tv(listing)
        self.assertEqual(members, 2)
        self.assertEqual(raw, 2097156)
        self.assertEqual(names, ["zeros.bin", "ok.txt"])

    def test_zstd_ratio_via_origin_override(self) -> None:
        fake = self.dir / "fakebsdtar"
        fake.write_text(
            "#!/bin/sh\necho '-rw-r--r--  0 u g  2097152 Jan  1  2026 zeros.bin' >&2\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        p = self.dir / "bomb.tar.zst"
        p.write_bytes(cam.ZSTD_MAGIC + b"\x00\x00")
        old = os.environ.get("SERA_SANDBOX_BSDTAR")
        os.environ["SERA_SANDBOX_BSDTAR"] = str(fake)
        try:
            self.assertEqual(cam.check_path(p), 3)
        finally:
            if old is None:
                os.environ.pop("SERA_SANDBOX_BSDTAR", None)
            else:
                os.environ["SERA_SANDBOX_BSDTAR"] = old


if __name__ == "__main__":
    unittest.main()
