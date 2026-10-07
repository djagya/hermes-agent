"""The image's install stamp names the same tool store as the image ENV.

Processes started without the container environment (an s6 service whose run
script is a plain ``#!/bin/sh``, ``docker exec env -i``) have no
``HERMES_RUNTIME_DIR``. They must still resolve the sealed store from the stamp,
or PM reports the baked ffmpeg/node/npm/ripgrep as "not installed or outdated".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from hermes_constants import get_default_hermes_root
from pm.environments import store_root

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def _final_stage() -> str:
    return DOCKERFILE.read_text(encoding="utf-8").rsplit("AS runtime\n", 1)[1]


def test_stamped_runtime_dir_matches_the_runtime_env():
    text = DOCKERFILE.read_text(encoding="utf-8")
    stamped = re.findall(r'stamp\["runtimeDir"\] = "([^"]+)"', text)
    env = re.findall(r"HERMES_RUNTIME_DIR=(\S+)", _final_stage())
    assert stamped and env
    assert set(stamped) == set(env)


def test_stamp_runtime_dir_wins_without_the_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    install = tmp_path / "opt-hermes"
    install.mkdir()
    tools = install / "tools"
    tools.mkdir()
    # Shape of the Dockerfile's stamp: external, no manifest, absolute runtimeDir.
    (install / "install-stamp.json").write_text(json.dumps({
        "distribution": "docker", "updateMechanism": "external",
        "pmRuntime": str(install / "pm-runtime"), "runtimeDir": str(tools),
    }))
    assert store_root(install) == tools.resolve()
    assert store_root(install) != get_default_hermes_root() / "tools"
