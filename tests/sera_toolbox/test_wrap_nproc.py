"""wrap must not ulimit -u in the parent (busy hermes UID)."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRAP = ROOT / "docker/sera-toolbox/wrap"


class WrapNprocParent(unittest.TestCase):
    def test_no_parent_ulimit_nproc(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        # Inner bwrap cap stays. Parent `ulimit -u "$nproc_lim"` must not.
        self.assertIn('ulimit -u "$1"', text)
        self.assertNotIn('ulimit -u "$nproc_lim"', text)
        self.assertIn("Do NOT ulimit -u in this parent", text)

    def test_bwrap_cpu_rlimit_not_parent(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        self.assertIn('--rlimit-cpu', text)
        self.assertNotRegex(text, r'ulimit -t ["$]')

    def test_bounded_tmpfs(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        self.assertIn("SERA_SANDBOX_TMPFS_BYTES", text)
        self.assertIn('--size "$tmpfs_bytes" --tmpfs /tmp', text)

    def test_receipt_has_limits_and_io_hashes(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        self.assertIn('"limits"', text)
        self.assertIn('"inputs"', text)
        self.assertIn('"outputs"', text)
        self.assertIn("out_max_bytes", text)
        self.assertNotIn('"files": paths', text)

    def test_receipt_python_compiles(self) -> None:
        text = WRAP.read_text(encoding="utf-8")
        start = text.index("import hashlib, json, os, sys, time")
        end = text.index("PY\ndrop_path \"$tmp_before\"", start)
        snippet = text[start:end]
        compile(snippet, "wrap-receipt", "exec")


if __name__ == "__main__":
    unittest.main()
