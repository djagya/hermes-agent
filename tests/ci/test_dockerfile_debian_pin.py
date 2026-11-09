"""dlz: one Debian base and one apt snapshot across the image build.

sqlite_build compiles the libsqlite that runtime_base and the published runtime
stage load, so the three `FROM debian:` stages must share one digest (a forward
merge once left sqlite_build on another base). The snapshot that freezes apt
must also agree everywhere the build and the smoke read it.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
SNAPSHOT = re.compile(r"\b(20\d{6}T\d{6}Z)\b")


def test_every_debian_stage_uses_one_pinned_digest():
    bases = re.findall(r"^FROM (debian:\S+)", DOCKERFILE, re.M)
    assert len(bases) >= 3, bases
    assert all(re.fullmatch(r"debian:[\w.-]+@sha256:[0-9a-f]{64}", b) for b in bases), bases
    assert len(set(bases)) == 1, sorted(set(bases))


def test_debian_snapshot_agrees_across_build_and_smoke():
    args = re.findall(r"^ARG DEBIAN_SNAPSHOT=(\S+)$", DOCKERFILE, re.M)
    assert len(args) >= 2 and len(set(args)) == 1, args
    for rel in (
        ".github/workflows/fork-release-image.yml",
        "docker/sera-toolbox/BUILD.md",
        "docker/sera-toolbox/smoke.sh",
    ):
        found = set(SNAPSHOT.findall((ROOT / rel).read_text(encoding="utf-8")))
        assert found == set(args), (rel, sorted(found))
