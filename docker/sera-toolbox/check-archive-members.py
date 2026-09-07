#!/usr/bin/env python3
"""Refuse zip-slip, tar-slip, and archive bombs before bwrap extract.

Exit 2 = traversal / absolute member. Exit 3 = bomb (ratio, size, count).
Non-archive args are ignored so wrapper flags stay usable.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

SEVEN_Z_MAGIC = b"7z\xbc\xaf'\x1c"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _limits() -> tuple[float, int, int]:
    max_ratio = float(os.environ.get("SERA_SANDBOX_ZIP_RATIO", "100"))
    max_uncomp = int(os.environ.get("SERA_SANDBOX_ZIP_MAX_BYTES", str(64 * 1024 * 1024)))
    max_members = int(os.environ.get("SERA_SANDBOX_ZIP_MAX_MEMBERS", "4096"))
    return max_ratio, max_uncomp, max_members


def bad_name(name: str) -> bool:
    if not name:
        return False
    norm = name.replace("\\", "/")
    if norm.startswith("/") or norm.startswith("~/"):
        return True
    return any(part == ".." for part in norm.split("/"))


def check_zip(path: Path) -> int:
    max_ratio, max_uncomp, max_members = _limits()
    with zipfile.ZipFile(path) as z:
        raw = 0
        comp = 0
        members = 0
        for info in z.infolist():
            members += 1
            if members > max_members:
                return 3
            if bad_name(info.filename):
                return 2
            raw += info.file_size
            comp += info.compress_size
        if raw > max_uncomp:
            return 3
        if comp and (raw / comp) > max_ratio:
            return 3
    return 0


def check_tar(path: Path) -> int:
    max_ratio, max_uncomp, max_members = _limits()
    with tarfile.open(path, "r:*") as t:
        raw = 0
        members = 0
        for info in t.getmembers():
            members += 1
            if members > max_members:
                return 3
            if bad_name(info.name):
                return 2
            if info.issym() or info.islnk():
                if bad_name(info.linkname or ""):
                    return 2
            raw += max(info.size, 0)
        if raw > max_uncomp:
            return 3
        # Compressed size is the archive file. Same bomb ratio as zip.
        comp = path.stat().st_size
        if comp and (raw / comp) > max_ratio:
            return 3
    return 0


def _real_7z() -> str | None:
    # Never PATH `7z` — that is the wrap. Origin / libexec copy / test override.
    override = os.environ.get("SERA_SANDBOX_7Z", "")
    if override:
        return override
    origin = Path("/usr/libexec/sera-toolbox/7z.origin")
    if origin.is_file():
        real = origin.read_text(encoding="utf-8").strip()
        if real and Path(real).is_file():
            return real
    copied = Path("/usr/libexec/sera-toolbox/7z")
    if copied.is_file() and not copied.is_symlink():
        return str(copied)
    return None


def parse_7z_slt(text: str, skip_name: str = "") -> tuple[int, int, int, list[str]]:
    raw = 0
    packed = 0
    members = 0
    names: list[str] = []
    size = 0
    packed_size = 0
    name = ""

    def flush() -> None:
        nonlocal raw, packed, members, name, size, packed_size
        if not name:
            return
        if skip_name and Path(name).name == Path(skip_name).name:
            name = ""
            size = 0
            packed_size = 0
            return
        members += 1
        raw += size
        packed += packed_size
        names.append(name)
        name = ""
        size = 0
        packed_size = 0

    for line in text.splitlines():
        if line.startswith("Path = "):
            flush()
            name = line.split(" = ", 1)[1]
            size = 0
            packed_size = 0
        elif line.startswith("Size = "):
            try:
                size = int(line.split(" = ", 1)[1])
            except ValueError:
                size = 0
        elif line.startswith("Packed Size = "):
            try:
                packed_size = int(line.split(" = ", 1)[1])
            except ValueError:
                packed_size = 0
    flush()
    return raw, packed, members, names


def check_7z(path: Path) -> int:
    max_ratio, max_uncomp, max_members = _limits()
    real = _real_7z()
    if not real:
        return 0
    try:
        proc = subprocess.run(
            [real, "l", "-slt", "--", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 3
    raw, packed, members, names = parse_7z_slt(proc.stdout, skip_name=path.name)
    if members > max_members:
        return 3
    if any(bad_name(n) for n in names):
        return 2
    if raw > max_uncomp:
        return 3
    if packed and (raw / packed) > max_ratio:
        return 3
    return 0


def _real_bsdtar() -> str | None:
    override = os.environ.get("SERA_SANDBOX_BSDTAR", "")
    if override:
        return override
    origin = Path("/usr/libexec/sera-toolbox/bsdtar.origin")
    if origin.is_file():
        real = origin.read_text(encoding="utf-8").strip()
        if real and Path(real).is_file():
            return real
    copied = Path("/usr/libexec/sera-toolbox/bsdtar")
    if copied.is_file() and not copied.is_symlink():
        return str(copied)
    return None


def parse_bsdtar_tv(text: str) -> tuple[int, int, list[str]]:
    raw = 0
    members = 0
    names: list[str] = []
    for line in text.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        try:
            size = int(parts[4])
        except ValueError:
            continue
        name = parts[8]
        members += 1
        raw += max(size, 0)
        names.append(name)
    return raw, members, names


def check_zstd(path: Path) -> int:
    max_ratio, max_uncomp, max_members = _limits()
    real = _real_bsdtar()
    if not real:
        return 0
    try:
        proc = subprocess.run(
            [real, "-tvf", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 3
    raw, members, names = parse_bsdtar_tv(proc.stderr or proc.stdout)
    if members > max_members:
        return 3
    if any(bad_name(n) for n in names):
        return 2
    if raw > max_uncomp:
        return 3
    packed = path.stat().st_size
    if packed and (raw / packed) > max_ratio:
        return 3
    return 0


def check_path(path: Path) -> int:
    if not path.is_file():
        return 0
    if zipfile.is_zipfile(path):
        return check_zip(path)
    if tarfile.is_tarfile(path):
        return check_tar(path)
    head = path.read_bytes()[:6]
    if head.startswith(SEVEN_Z_MAGIC):
        return check_7z(path)
    if head.startswith(ZSTD_MAGIC):
        return check_zstd(path)
    return 0


def main(argv: list[str]) -> int:
    for arg in argv:
        code = check_path(Path(arg))
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
