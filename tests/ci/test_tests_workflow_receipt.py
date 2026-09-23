"""The Python test workflow publishes the runner's receipt for every slice.

The receipt the runner writes is only external evidence if CI keeps it. The
contract: the step that runs the suite names a receipt path, and an upload
step that runs even when the suite fails publishes exactly that path under a
per-slice artifact name (so slices don't overwrite each other). A missing
receipt, or one that disagrees with the run step or the candidate, fails
the job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _test_job_steps() -> list[dict]:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    )
    return doc["jobs"]["test"]["steps"]


def test_every_slice_uploads_the_receipt_the_runner_writes() -> None:
    steps = _test_job_steps()
    [run_step] = [s for s in steps if "scripts/run_tests.sh" in s.get("run", "")]
    receipt_path = run_step["env"]["HERMES_TEST_RECEIPT"]
    assert run_step["env"]["HERMES_CANDIDATE_SHA"]

    uploads = [
        s for s in steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
        and s.get("with", {}).get("path") == receipt_path
    ]
    assert len(uploads) == 1, "exactly one upload of the runner's receipt path"
    [upload] = uploads
    assert steps.index(upload) > steps.index(run_step)
    assert "always()" in str(upload.get("if", "")), "red slices must still publish"
    assert "${{" in upload["with"]["name"], "artifact name must be per-slice"
    # Missing receipt fails the job instead of warning.
    assert upload["with"].get("if-no-files-found") == "error"
    assert not upload.get("continue-on-error")


def test_receipt_is_checked_against_the_run_step_and_candidate() -> None:
    steps = _test_job_steps()
    [run_step] = [s for s in steps if "scripts/run_tests.sh" in s.get("run", "")]
    run_id = run_step["id"]
    receipt_path = run_step["env"]["HERMES_TEST_RECEIPT"]
    [check] = [s for s in steps if "scripts/run_tests_receipt.py check" in s.get("run", "")]
    assert steps.index(check) > steps.index(run_step)
    assert receipt_path in check["run"]
    assert not check.get("continue-on-error")
    # Runs after a red run step too, and binds to that step's outcome and the
    # same candidate expression the run step declared.
    assert f"steps.{run_id}.outcome" in str(check.get("if", ""))
    assert check["env"]["RUN_OUTCOME"] == f"${{{{ steps.{run_id}.outcome }}}}"
    assert check["env"]["EXPECTED_CANDIDATE"] == run_step["env"]["HERMES_CANDIDATE_SHA"]
