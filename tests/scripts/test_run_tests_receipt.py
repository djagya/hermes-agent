"""The runner's --receipt is a truthful record of the run it describes.

A receipt exists so a reviewer can accept or reject a test claim without
re-reading console output. That only works if it cannot say "pass" for a run
that proved nothing, and cannot flatten a collection/import failure into
"tests failed". Each test drives the real runner (scripts/run_tests_parallel.py)
over a synthetic probe file and asserts on the JSON it wrote:

- positive control: a passing file yields verdict ``pass`` with the candidate
  commit, the checkout it actually imported, and the per-file phase;
- negative controls: a -k that matches nothing yields ``no_tests`` (never
  ``pass``); an assertion failure yields ``fail`` / ``tests_failed``; an
  import error yields ``fail`` / ``interrupted`` — not behavioural RED;
- a flake that self-heals on retry is recorded as flaky with both exit codes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "run_tests_parallel.py"


def _run(
    tmp_path: Path, probe_body: str, *extra: str, env: dict | None = None
) -> tuple[subprocess.CompletedProcess, dict]:
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "test_receipt_probe.py").write_text(probe_body, encoding="utf-8")
    receipt_path = tmp_path / "out" / "receipt.json"
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--paths", str(probe_dir), "-j", "1",
         "--file-timeout", "60", "--file-retries", "0",
         "--receipt", str(receipt_path), *extra],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    assert receipt_path.is_file(), f"no receipt written:\n{proc.stdout}"
    return proc, json.loads(receipt_path.read_text(encoding="utf-8"))


_PASSING = "def test_alpha():\n    assert True\n\ndef test_beta():\n    assert True\n"


def test_passing_run_receipt_carries_candidate_scope_and_import_provenance(tmp_path: Path) -> None:
    proc, receipt = _run(tmp_path, _PASSING)
    assert proc.returncode == 0, proc.stdout
    assert receipt["verdict"] == "pass"
    assert receipt["exit_code"] == proc.returncode
    assert receipt["claim_level"] == "component"

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    assert receipt["candidate"]["commit"] == head

    imported = receipt["environment"]["import"]
    assert imported["matches_checkout"] is True, imported
    assert Path(imported["path"]).resolve().is_relative_to(REPO_ROOT.resolve())

    assert receipt["totals"]["passed"] == 2
    assert receipt["totals"]["collected"] == 2
    [record] = receipt["files"]
    assert record["phase"] == "passed"
    assert record["exit_codes"] == [0]
    assert receipt["invocation"]["mode"] == "discover"


def test_run_identity_is_recorded_but_hidden_from_the_tests(tmp_path: Path) -> None:
    """Receipt-only identity must not change what the verified tests see."""
    env = {
        **os.environ,
        "HERMES_CANDIDATE_SHA": "0" * 40,
        "GITHUB_SERVER_URL": "https://ci.example.invalid",
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_RUN_ID": "12345",
    }
    body = (
        "import os\n\n"
        "def test_identity_not_leaked():\n"
        "    for k in ('HERMES_CANDIDATE_SHA', 'GITHUB_RUN_ID', 'GITHUB_REPOSITORY'):\n"
        "        assert k not in os.environ, k\n"
    )
    proc, receipt = _run(tmp_path, body, env=env)
    assert proc.returncode == 0, proc.stdout
    assert receipt["verdict"] == "pass"
    assert receipt["candidate"]["declared_candidate"] == "0" * 40
    assert receipt["ci"]["run_url"] == "https://ci.example.invalid/example/repo/actions/runs/12345"


def test_zero_collected_receipt_is_no_tests_not_pass(tmp_path: Path) -> None:
    proc, receipt = _run(tmp_path, _PASSING, "-k", "zzz_matches_nothing")
    assert proc.returncode == 1, proc.stdout
    assert receipt["verdict"] == "no_tests"
    assert receipt["totals"]["collected"] == 0
    assert receipt["totals"].get("deselected") == 2
    # The per-file rc=5 is tolerated by the runner; the receipt keeps the
    # raw phase so the tolerance can't hide what happened.
    [record] = receipt["files"]
    assert record["exit_codes"] == [5]
    assert record["phase"] == "no_tests_collected"
    assert receipt["invocation"]["pytest_passthrough"] == ["-k", "zzz_matches_nothing"]


def test_assertion_failure_is_tests_failed(tmp_path: Path) -> None:
    proc, receipt = _run(tmp_path, "def test_red():\n    assert 1 == 2\n")
    assert proc.returncode == 1, proc.stdout
    assert receipt["verdict"] == "fail"
    [record] = receipt["files"]
    assert record["phase"] == "tests_failed"
    assert record["counts"]["failed"] == 1


def test_import_error_is_not_behavioural_red(tmp_path: Path) -> None:
    proc, receipt = _run(
        tmp_path, "import hermes_module_that_does_not_exist\n\ndef test_x():\n    assert True\n"
    )
    assert proc.returncode == 1, proc.stdout
    [record] = receipt["files"]
    assert record["phase"] == "interrupted", record
    assert record["phase"] != "tests_failed"


def test_self_healed_flake_is_recorded_with_both_attempts(tmp_path: Path) -> None:
    marker = tmp_path / "ran-once"
    body = (
        "from pathlib import Path\n\n"
        "def test_flaky_once():\n"
        f"    marker = Path({str(marker)!r})\n"
        "    if not marker.exists():\n"
        "        marker.write_text('x')\n"
        "        assert False, 'first attempt'\n"
    )
    proc, receipt = _run(tmp_path, body, "--file-retries", "1")
    assert proc.returncode == 0, proc.stdout
    [record] = receipt["files"]
    assert record["exit_codes"] == [1, 0]
    assert record["flaky"] is True
    assert receipt["flaky_files"] == [record["path"]]


def _load_receipt_module():
    spec = importlib.util.spec_from_file_location(
        "run_tests_receipt", REPO_ROOT / "scripts" / "run_tests_receipt.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_import_provenance_flags_a_foreign_checkout(tmp_path: Path) -> None:
    """A module resolved outside the checkout is reported as a mismatch."""
    mod = _load_receipt_module()
    foreign = tmp_path / "other_checkout"
    foreign.mkdir()
    (foreign / "probe_mod.py").write_text("", encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env = {"PATH": "", "PYTHONPATH": str(foreign)}
    got = mod.import_provenance(sys.executable, checkout, env, module="probe_mod")
    assert got["matches_checkout"] is False, got
    assert Path(got["path"]).parent == foreign.resolve()

    missing = mod.import_provenance(sys.executable, checkout, env, module="absent_mod_xyz")
    assert missing["matches_checkout"] is None
    assert "reason" in missing
