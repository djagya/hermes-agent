"""pnpm stages from two archives into one entry: the native executable and its launcher's dist/.

pnpm 12's executable looks for the bundled node-gyp beside itself (``dist/node-gyp-bin``) to build
dependencies that ship a binding.gyp; the launcher package carries that dist/. The executable's own
manifest must survive the merge: node-deps reads pnpm's version from the package.json beside it.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from pm import Lockfile, get_package, paths
from pm.install import _prepare_artifacts
from pm.store import ALL_TARGETS


def _tarball(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(content)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(content))
    return path


class _LocalArchives:
    """A store whose downloads are the given local archives, in artifact order."""

    def __init__(self, archives: list[Path]):
        self.archives = archives

    def fetch_many(self, artifacts, scratch, **_kwargs):
        return self.archives[:len(artifacts)]


def test_every_target_with_a_pinned_node_can_install_the_js_workspaces():
    """The dashboard, TUI and desktop source builds all install through pnpm on PM's node, so a
    target with a node pin but no pnpm pin cannot build them (Termux: "unavailable on
    linux-arm64-bionic" from `hermes dashboard`)."""
    package = get_package("pnpm")
    lock = Lockfile(paths.lockfile_path())
    version = lock.version("pnpm")
    for target in ALL_TARGETS:
        if not lock.artifacts("node", target):
            continue
        assert package.missing_reason(target) is None, target
        assert [a["url"] for a in lock.artifacts("pnpm", target)] == package.fetch_urls(version, target), target


def test_a_bare_install_provisions_pnpm_only_where_it_builds_the_frontends(monkeypatch):
    from pm import registry, store

    names = Lockfile(paths.lockfile_path()).names()
    monkeypatch.setattr(store, "current_target", lambda: "linux-x64")
    assert "pnpm" in registry.source_install_packages(names)
    monkeypatch.setattr(store, "current_target", lambda: "linux-arm64-bionic")
    assert "pnpm" not in registry.source_install_packages(names)


def test_pm_doctor_on_termux_does_not_fail_on_the_pnpm_it_fetches_on_demand(tmp_path, monkeypatch, capsys):
    import argparse

    from pm import cli

    lock = json.loads(paths.lockfile_path().read_text(encoding="utf-8"))
    lock["packages"] = {name: lock["packages"][name] for name in ("node", "pnpm")}
    (tmp_path / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setattr(cli, "_lockfile", lambda: Lockfile(tmp_path / "lock.json"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(cli, "current_target", lambda: "linux-arm64-bionic")
    monkeypatch.setattr(cli, "_doctor_uv_cache", lambda **_: 0)

    cli.cmd_doctor(argparse.Namespace())

    lines = capsys.readouterr().out.splitlines()
    assert lines == ["✗ node: not installed", "? pnpm: not installed"]


@pytest.mark.parametrize("distribution,required", [(None, True), ("docker", False), ("nix", True)])
def test_a_docker_image_does_not_require_the_pnpm_it_never_runs(distribution, required, tmp_path, monkeypatch):
    """dlz: the runtime image ships prebuilt frontends and no pnpm, so every `hermes` launch in it printed
    "install out of sync (pnpm: not installed or outdated)". Its stamp's distribution makes pnpm on demand
    there; an unstamped checkout and other distributions still require it."""
    import pm.install

    lock = json.loads(paths.lockfile_path().read_text(encoding="utf-8"))
    lock["packages"] = {name: lock["packages"][name] for name in ("node", "pnpm")}
    (tmp_path / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (tmp_path / "facts.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pm.install, "_lockfile", lambda: Lockfile(tmp_path / "lock.json"))
    monkeypatch.setattr(pm.install, "_facts", dict)
    monkeypatch.setattr(paths, "facts_path", lambda: tmp_path / "facts.json")
    monkeypatch.setattr(pm.install, "current_target", lambda: "linux-x64")
    # The image records node (and the rest of its tools), never pnpm.
    monkeypatch.setattr(pm.install, "_installed_location",
                        lambda package, _lock, _target: (None, None) if package.name == "node" else None)
    image = tmp_path / "image"
    image.mkdir()
    if distribution:
        stamp = {"schemaVersion": 2, "distribution": distribution, "updateMechanism": "external"}
        (image / "install-stamp.json").write_text(json.dumps(stamp), encoding="utf-8")
    monkeypatch.setenv("HERMES_INSTALL_ROOT", str(image))

    assert pm.install.check(include_venv=False) == (["pnpm: not installed or outdated"] if required else [])
    assert get_package("pnpm").is_optional("linux-x64") is not required


@pytest.mark.parametrize("target", ["linux-x64", "win32-x64", "linux-arm64-bionic"])
def test_pnpm_entry_holds_the_executable_and_the_launchers_dist(tmp_path, target):
    package = get_package("pnpm")
    lock = Lockfile(paths.lockfile_path())
    version = lock.version("pnpm")
    artifacts = lock.artifacts("pnpm", target)
    # Extraction follows the lock's artifact order: the executable first, then the launcher.
    assert [artifact["url"] for artifact in artifacts] == package.fetch_urls(version, target)
    executable = package.binary_rel["win32" if target.startswith("win32") else "posix"]
    exe_name = artifacts[0]["url"].rsplit("/-/", 1)[0].removeprefix("https://registry.npmjs.org/")
    exe = _tarball(tmp_path / "exe.tgz", {
        executable: b"native pnpm", "package.json": json.dumps({"name": exe_name, "version": version}).encode(),
        "LICENSE": b"license"})
    launcher = _tarball(tmp_path / "launcher.tgz", {
        "package.json": json.dumps({"name": "pnpm", "version": version}).encode(), "bin/pnpm.cjs": b"launcher",
        "dist/pnpm.cjs": b"bundle", "dist/node-gyp-bin/node-gyp": b"#!/bin/sh\n",
        "dist/node_modules/node-gyp/package.json": b'{"name": "node-gyp"}'})
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    staged = _prepare_artifacts(package, _LocalArchives([exe, launcher]), scratch, artifacts, version, target)

    assert package.binary(staged, target).read_bytes() == b"native pnpm"
    assert (staged / "dist/node-gyp-bin/node-gyp").is_file()
    assert (staged / "dist/node_modules/node-gyp/package.json").is_file()
    assert json.loads((staged / "package.json").read_text(encoding="utf-8"))["name"] == exe_name
    # Only the launcher's dist/ is kept: its JS entry point never shadows the executable.
    assert not (staged / "bin").exists()
    assert not (staged / "package").exists()


def test_pnpm_dirs_replace_every_case_twin_of_their_names():
    """Windows hands Python upper-cased env names, and a child given both spellings picks one
    arbitrarily: the Hermes-owned store must be the only spelling left."""
    from pm.packages import pnpm_env

    env = pnpm_env({"PNPM_CONFIG_STORE_DIR": "/elsewhere/store", "Pnpm_Config_Cache_Dir": "/elsewhere/cache"})

    assert [key for key in env if key.upper().startswith("PNPM_CONFIG_")] == [
        "pnpm_config_store_dir", "pnpm_config_cache_dir", "pnpm_config_state_dir"]
    assert "/elsewhere" not in "".join(env.values())
