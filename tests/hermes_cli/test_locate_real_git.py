"""``_locate_real_git`` must find git-core under any Git-for-Windows architecture directory.

Git for Windows v2.56.0 moved from MINGW64 to UCRT64 (``/mingw64/bin/git.exe`` no longer exists),
so the fixed ``clangarm64`` / ``mingw64`` / ``mingw32`` list finds nothing on a current install.
The Program Files roots are mapped onto a temp tree; the layout is data, the host is not faked.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd_git

_ROOTS = (r"C:\Program Files", r"C:\Program Files (x86)")


@pytest.mark.parametrize("arch_dirs,expected", [
    (["ucrt64"], "ucrt64"),                    # Git for Windows >= 2.56 x64
    (["clangarm64", "ucrt64"], "clangarm64"),  # known names keep their priority
    (["mingw64"], "mingw64"),                  # older installs unchanged
])
def test_locate_real_git_finds_any_arch_dir(tmp_path, arch_dirs, expected):
    program_files = tmp_path / "Program Files"
    for arch in arch_dirs:
        git = program_files / "Git" / arch / "libexec" / "git-core" / "git.exe"
        git.parent.mkdir(parents=True)
        git.write_bytes(b"")
    probed = []

    def fake_path(*parts):
        if parts and parts[0] in _ROOTS:
            return program_files if parts[0] == _ROOTS[0] else tmp_path / "missing"
        return Path(*parts)

    def probe(cmd):
        probed.append(cmd[0])
        return False  # every candidate runs cleanly

    with (
        patch.object(update_cmd_git, "Path", side_effect=fake_path),
        patch.object(update_cmd_git, "_portable_git_candidates", return_value=[]),
        patch.object(update_cmd_git, "_probe_fork_bomb", side_effect=probe),
    ):
        found = update_cmd_git._locate_real_git()

    assert found == program_files / "Git" / expected / "libexec" / "git-core" / "git.exe"
    assert probed == [str(found)]
