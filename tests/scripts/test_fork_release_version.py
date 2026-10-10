"""scripts/fork-release-version.sh: derived version and release-branch name check."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fork-release-version.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.21.5"\n'
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "release")
    _git(tmp_path, "tag", "v2026.9.24")
    (tmp_path / "f").write_text("fork\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "fork")
    return tmp_path


def _run(
    repo: Path, *args: str, branch: str | None = None
) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GITHUB_REF_NAME", "RELEASE_BRANCH")
    }
    if branch is not None:
        env["RELEASE_BRANCH"] = branch
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=repo, env=env, capture_output=True, text=True
    )


def test_derives_base_and_distance(repo: Path) -> None:
    out = _run(repo).stdout
    assert "tag=v2026.9.24\nbase=0.21.5\ndistance=1\n" in out
    assert "display=0.21.5+1.g" in out


@pytest.mark.parametrize(
    "branch",
    [
        "release/v0.21.5-dlz",
        "release/v0.21-dlz",
        "release/v0.21.5",
        "release/other",
        "main",
    ],
)
def test_check_accepts_matching_or_unversioned_branch(repo: Path, branch: str) -> None:
    assert _run(repo, "--check", branch=branch).returncode == 0


@pytest.mark.parametrize(
    "branch", ["release/v0.21.4-upstream-dlz", "release/v0.22-dlz"]
)
def test_check_refuses_branch_named_for_another_version(
    repo: Path, branch: str
) -> None:
    result = _run(repo, "--check", branch=branch)
    assert result.returncode == 1
    assert "this tree ships 0.21.5" in result.stderr
    assert "release/v0.21.5-dlz" in result.stderr


def test_branch_check_only_with_check_flag(repo: Path) -> None:
    assert _run(repo, branch="release/v0.21.4-upstream-dlz").returncode == 0


def _merge_semver_release(repo: Path, version: str) -> None:
    """Upstream since v0.21.6: main carries 0.0.0, the release is a semver tag (plus canaries)."""
    _git(repo, "checkout", "-q", "-b", "upstream", "HEAD~1")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.0.0"\n')
    _git(repo, "commit", "-q", "-am", "main carries 0.0.0")
    _git(repo, "tag", "v0.21.5+canary.20261008T070449Z")
    (repo / "u").write_text("upstream\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", f"v{version}")
    (repo / "after").write_text("canary\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "canary")
    _git(repo, "tag", f"v{version}+canary.20261009T070410Z")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--no-edit", "-X", "ours", f"v{version}")
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    _git(repo, "commit", "-q", "-am", "fork keeps the base")


def test_semver_release_tag_wins_over_calver(repo: Path) -> None:
    _merge_semver_release(repo, "0.21.6")
    out = _run(repo, "--check", branch="release/v0.21.6-dlz")
    assert out.returncode == 0, out.stderr
    assert "tag=v0.21.6\nbase=0.21.6\n" in out.stdout
    # A canary past the release is not merged here, and never counts as a release anyway.
    assert "canary" not in out.stdout


def test_semver_release_refuses_the_old_branch_name(repo: Path) -> None:
    _merge_semver_release(repo, "0.21.6")
    result = _run(repo, "--check", branch="release/v0.21.5-dlz")
    assert result.returncode == 1
    assert "this tree ships 0.21.6" in result.stderr
