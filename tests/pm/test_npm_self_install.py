"""npm's self-install keeps its cache out of the store's download entry.

A cache beside the archive turned the entry's removal into a tree delete
that failed on Windows while Defender held the fresh tarball copy
(``[WinError 145] The directory is not empty: ...fetch-<sha>\\.npm-cache``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import pm.install
import pm.packages
from pm.registry import get_package
from pm.store import Store, current_target


def _fake_node(store: Store, target: str) -> None:
    node_bin = get_package("node").binary(store.entry("node-1"), target)
    node_bin.parent.mkdir(parents=True, exist_ok=True)
    node_bin.write_bytes(b"node")
    cli_root = node_bin.parent if target.startswith("win32") else node_bin.parent.parent / "lib"
    cli = cli_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")


def test_npm_self_install_leaves_download_entry_holding_only_the_archive(tmp_path, monkeypatch):
    target = current_target()
    store = Store(tmp_path / "store")
    _fake_node(store, target)
    monkeypatch.setattr(pm.install, "_lockfile", lambda: None)
    monkeypatch.setattr(pm.install, "_installed_location",
                        lambda package, lockfile, target: ({"node": {"entry": "node-1"}}, store))

    caches: list[Path] = []

    def fake_npm(cmd, **kwargs):
        # Stand in for npm: cacache writes the installed tarball into the cache.
        cache = Path(kwargs["env"]["npm_config_cache"])
        content = cache / "_cacache" / "content-v2" / "sha512" / "b8" / "85"
        content.mkdir(parents=True)
        (content / "blob").write_bytes(b"tarball")
        caches.append(cache)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pm.packages.subprocess, "run", fake_npm)

    entry = store.entry("fetch-" + "a" * 64)
    entry.mkdir(parents=True)
    archive = entry / "npm-1.0.0.tgz"
    archive.write_bytes(b"archive")
    staged = tmp_path / "scratch" / "tree"

    get_package("npm").unpack(archive, staged, target)

    assert [p.name for p in entry.iterdir()] == [archive.name]
    assert caches and not caches[0].is_relative_to(staged)


@pytest.mark.platforms("posix")
def test_npm_self_install_runs_the_grafted_managed_npm_cli(tmp_path, monkeypatch):
    """The image replaces node's bundled npm with the managed tree; unpack still runs its cli."""
    from scripts.bundles.payload import graft_npm

    target = current_target()
    store = Store(tmp_path / "store")
    _fake_node(store, target)
    node_bin = get_package("node").binary(store.entry("node-1"), target)
    managed = tmp_path / "managed-npm"
    package = managed / "lib/node_modules/npm"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "npm-cli.js").write_text("// 12.2.2\n", encoding="utf-8")
    (package / "package.json").write_text('{"name": "npm", "version": "12.2.2"}', encoding="utf-8")
    graft_npm(node_bin.parent.parent, managed)
    monkeypatch.setattr(pm.install, "_lockfile", lambda: None)
    monkeypatch.setattr(pm.install, "_installed_location",
                        lambda package, lockfile, target: ({"node": {"entry": "node-1"}}, store))
    commands: list[list[str]] = []

    def fake_npm(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pm.packages.subprocess, "run", fake_npm)
    archive = store.entry("fetch-" + "b" * 64) / "npm-12.2.2.tgz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"archive")

    get_package("npm").unpack(archive, tmp_path / "scratch" / "tree", target)

    cli = Path(commands[0][1])
    assert cli.read_text(encoding="utf-8") == "// 12.2.2\n"
