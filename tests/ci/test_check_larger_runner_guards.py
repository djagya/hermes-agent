"""Behavioral tests for the larger-runner CI guard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "ci"
    / "check_larger_runner_guards.py"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_current_repo_passes():
    result = _run(REPO_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No unguarded larger runners." in result.stdout


def test_unguarded_larger_runner_fails(tmp_path: Path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "tests.yml").write_text(
        "name: Tests\n"
        "on: workflow_call\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest-96-core\n"
        "    steps:\n"
        "      - run: echo hi\n",
        encoding="utf-8",
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "ubuntu-latest-96-core" in result.stdout
    assert "tests.yml" in result.stdout


def test_nous_guard_allows_larger_runner(tmp_path: Path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "docker.yml").write_text(
        "name: Docker\n"
        "on: pull_request\n"
        "jobs:\n"
        "  build:\n"
        "    if: github.repository == 'NousResearch/hermes-agent'\n"
        "    runs-on: ${{ matrix.runner }}\n"
        "    strategy:\n"
        "      matrix:\n"
        "        include:\n"
        "          - runner: ubuntu-latest-32-core\n"
        "    steps:\n"
        "      - run: echo hi\n",
        encoding="utf-8",
    )

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_standard_runners_pass(tmp_path: Path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yaml").write_text(
        "name: CI\n"
        "on: pull_request\n"
        "jobs:\n"
        "  detect:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n",
        encoding="utf-8",
    )

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No unguarded larger runners." in result.stdout


@pytest.mark.parametrize(
    "missing",
    ["website/**", "docs/**", "nix/**", "**/*.md"],
)
def test_fork_release_ignores_non_image_paths(missing: str):
    text = (REPO_ROOT / ".github" / "workflows" / "fork-release-image.yml").read_text(
        encoding="utf-8"
    )
    assert "paths-ignore:" in text
    assert missing in text


@pytest.mark.parametrize(
    "required",
    [
        "Dockerfile",
        "docker/",
        "tests/docker",
        "tests/sera_toolbox",
    ],
)
def test_fork_release_does_not_ignore_image_or_test_paths(required: str):
    text = (REPO_ROOT / ".github" / "workflows" / "fork-release-image.yml").read_text(
        encoding="utf-8"
    )
    ignore_block = text.split("paths-ignore:", 1)[1].split("workflow_dispatch:", 1)[0]
    assert required not in ignore_block
