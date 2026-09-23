"""Machine-origin verification receipt for scripts/run_tests_parallel.py.

The runner's console summary is written for humans and has been misread
before ("0 failed ... 100% complete" over a run that collected nothing). A
receipt is the same run's facts as a small versioned JSON document, so a
reviewer can check *what* was tested (candidate, checkout actually imported,
scope, filters, environment) and *how* it ended (raw pytest exit phase per
file) without trusting a restatement.

Every field is derived from execution. A fact the runner cannot observe is
recorded as ``None`` with a reason — never a fabricated zero or ``true``.

Claim level is fixed at ``component``: a test run proves the behaviour of
the code the tests exercised, never that a production entrypoint launches
or that anything was deployed. Promotion to production-composition or
deployment evidence belongs to the owner that observes those surfaces.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "hermes-test-receipt/1"
CLAIM_LEVEL = "component"

# Module imported to prove which checkout's project code the pytest
# subprocesses actually load. A root-level, dependency-free module: importing
# it has no side effects beyond path resolution.
PROVENANCE_MODULE = "hermes_constants"

# pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html)
# plus the runner's own timeout convention. Anything else is "unknown": a
# nonzero exit is not behavioural RED unless the phase says the tests ran
# and failed.
_EXIT_PHASES = {
    0: "passed",
    1: "tests_failed",
    2: "interrupted",
    3: "internal_error",
    4: "usage_error",
    5: "no_tests_collected",
    124: "timeout",
}

# GitHub-provided identity of the run that produced the receipt. None of
# these is a credential (GITHUB_TOKEN is never an ambient env var).
_CI_VARS = (
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_WORKFLOW",
    "GITHUB_JOB",
    "GITHUB_REF",
    "GITHUB_SHA",
    "GITHUB_EVENT_NAME",
    "GITHUB_SERVER_URL",
)

# Variables that exist only to describe the run in the receipt. The runner
# removes them from its own environment before any pytest child starts, so
# forwarding them through run_tests.sh's `env -i` allowlist does not change
# what the tests under verification see. HERMES_TEST_RECEIPT is included:
# a nested runner started by a test must not write its own receipt.
RECEIPT_ONLY_ENV = ("HERMES_TEST_RECEIPT", "HERMES_CANDIDATE_SHA") + _CI_VARS

_MAX_DIRTY_PATHS = 50


def take_run_identity(environ: Dict[str, str]) -> Dict[str, str]:
    """Pop the receipt-only variables out of ``environ`` and return them."""
    return {k: environ.pop(k) for k in RECEIPT_ONLY_ENV if environ.get(k)}


def classify_exit(rc: int) -> str:
    return _EXIT_PHASES.get(rc, "unknown")


def _git(repo_root: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_candidate(repo_root: Path, identity: Dict[str, str]) -> Dict[str, Any]:
    """Identity of the checkout the runner executed from.

    ``commit``/``tree`` are the tested checkout (on a PR this is GitHub's
    test-merge commit, not the branch head). ``declared_candidate`` is what
    the caller says it meant to test (HERMES_CANDIDATE_SHA); both are kept
    because they legitimately differ on pull_request runs.
    """
    commit = _git(repo_root, "rev-parse", "HEAD")
    tree = _git(repo_root, "rev-parse", "HEAD^{tree}")
    status = _git(repo_root, "status", "--porcelain", "--untracked-files=no")
    out: Dict[str, Any] = {
        "commit": commit.strip() if commit else None,
        "tree": tree.strip() if tree else None,
        "declared_candidate": identity.get("HERMES_CANDIDATE_SHA"),
    }
    if status is None:
        out["dirty"] = None
        out["dirty_reason"] = "git status unavailable"
    else:
        paths = [line[3:] for line in status.splitlines() if line.strip()]
        out["dirty"] = bool(paths)
        out["dirty_paths"] = paths[:_MAX_DIRTY_PATHS]
        out["dirty_count"] = len(paths)
    if commit is None:
        out["commit_reason"] = "not a git checkout or git unavailable"
    return out


def import_provenance(
    python: str, repo_root: Path, env: Dict[str, str], module: str = PROVENANCE_MODULE
) -> Dict[str, Any]:
    """Which file ``import <module>`` resolves to under the pytest env.

    Runs the same interpreter, cwd and environment the per-file pytest
    subprocesses get, so a worktree silently importing another checkout
    (editable install, PYTHONPATH) shows up as ``matches_checkout: false``.
    """
    code = f"import {module} as m; print(m.__file__)"
    try:
        proc = subprocess.run(
            [python, "-c", code],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"module": module, "path": None, "matches_checkout": None,
                "reason": f"probe failed: {type(exc).__name__}"}
    if proc.returncode != 0:
        tail = (proc.stderr.strip().splitlines() or ["(no stderr)"])[-1]
        return {"module": module, "path": None, "matches_checkout": None,
                "reason": f"import failed: {tail}"}
    origin = Path(proc.stdout.strip()).resolve()
    try:
        origin.relative_to(repo_root.resolve())
        matches = True
    except ValueError:
        matches = False
    return {"module": module, "path": str(origin), "matches_checkout": matches}


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _pytest_addopts(repo_root: Path) -> Optional[str]:
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return None
    try:
        data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts")


def _pytest_version(python: str, env: Dict[str, str]) -> Optional[str]:
    try:
        proc = subprocess.run(
            [python, "-c", "import pytest; print(pytest.__version__)"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def environment(python: str, repo_root: Path, env: Dict[str, str]) -> Dict[str, Any]:
    return {
        "python_executable": python,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "pytest_version": _pytest_version(python, env),
        "uv_lock_sha256": _sha256(repo_root / "uv.lock"),
        "pyproject_sha256": _sha256(repo_root / "pyproject.toml"),
        "import": import_provenance(python, repo_root, env),
    }


def ci_locator(identity: Dict[str, str]) -> Optional[Dict[str, str]]:
    found = {k: identity[k] for k in _CI_VARS if identity.get(k)}
    if not found:
        return None
    server = found.get("GITHUB_SERVER_URL")
    repo = found.get("GITHUB_REPOSITORY")
    run_id = found.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        found["run_url"] = f"{server}/{repo}/actions/runs/{run_id}"
    return found


def annotate_phase(record: Dict[str, Any]) -> Dict[str, Any]:
    """Add ``phase`` from the attempt that decided the file's outcome."""
    exits = record.get("exit_codes") or []
    if record.get("runner_error"):
        phase = "runner_crashed"
    elif exits:
        phase = classify_exit(exits[-1])
    else:
        phase = "unknown"
    return {**record, "phase": phase}


def verdict(exit_code: int, files_selected: int, collected: int) -> str:
    """Overall outcome. Only ``pass`` may satisfy a test claim."""
    if files_selected == 0 or collected == 0:
        return "no_tests"
    return "pass" if exit_code == 0 else "fail"


def build_receipt(
    *,
    repo_root: Path,
    python: str,
    pytest_env: Dict[str, str],
    identity: Dict[str, str],
    argv: List[str],
    scope: Dict[str, Any],
    exit_code: int,
    files: List[Dict[str, Any]],
    totals: Dict[str, int],
    flaky: List[str],
    omitted: List[Dict[str, str]],
) -> Dict[str, Any]:
    collected = sum(
        totals.get(k, 0)
        for k in ("passed", "failed", "skipped", "errors", "xfailed", "xpassed")
    )
    return {
        "schema": SCHEMA,
        "claim_level": CLAIM_LEVEL,
        "verdict": verdict(exit_code, len(files), collected),
        "exit_code": exit_code,
        "candidate": git_candidate(repo_root, identity),
        "invocation": {
            "argv": argv,
            "cwd": str(repo_root),
            **scope,
            "pytest_addopts_config": _pytest_addopts(repo_root),
        },
        "environment": environment(python, repo_root, pytest_env),
        "ci": ci_locator(identity),
        "totals": {**totals, "collected": collected, "files": len(files)},
        "flaky_files": flaky,
        "omitted": omitted,
        "files": [annotate_phase(f) for f in files],
    }


def write_receipt(path: Path, receipt: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def check_receipt(
    path: Path, *, step_outcome: str, expected_candidate: str
) -> List[str]:
    """Problems that make ``path`` unusable as evidence for the CI step.

    The binding CI relies on: the run step that produced ``step_outcome``
    wrote a receipt at ``path``, its exit code agrees with that outcome, and
    it names the candidate the workflow meant to test. A broken forwarding
    in run_tests.sh shows up here as a missing file or a missing
    ``declared_candidate``, not as a silently green job.
    """
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"receipt not readable at {path}: {type(exc).__name__}"]
    except ValueError:
        return [f"receipt at {path} is not valid JSON"]
    if not isinstance(receipt, dict):
        return [f"receipt at {path} is not a JSON object"]
    problems: List[str] = []
    if receipt.get("schema") != SCHEMA:
        problems.append(f"schema is {receipt.get('schema')!r}, expected {SCHEMA!r}")
    exit_code = receipt.get("exit_code")
    if step_outcome == "success" and exit_code != 0:
        problems.append(f"run step succeeded but receipt exit_code is {exit_code!r}")
    elif step_outcome == "failure" and exit_code in (0, None):
        problems.append(f"run step failed but receipt exit_code is {exit_code!r}")
    elif step_outcome not in ("success", "failure"):
        problems.append(f"unsupported step outcome {step_outcome!r}")
    declared = (receipt.get("candidate") or {}).get("declared_candidate")
    if declared != expected_candidate:
        problems.append(
            f"declared_candidate is {declared!r}, expected {expected_candidate!r}"
        )
    return problems


def _main(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Fail unless a test receipt binds to the CI step that wrote it."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("path", type=Path)
    check.add_argument("--step-outcome", required=True)
    check.add_argument("--expected-candidate", required=True)
    args = parser.parse_args(argv)
    problems = check_receipt(
        args.path,
        step_outcome=args.step_outcome,
        expected_candidate=args.expected_candidate,
    )
    for problem in problems:
        print(f"receipt check: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"receipt check: {args.path} binds to this step ({args.step_outcome})")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
