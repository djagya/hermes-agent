"""Regression tests for kernel-enforced Kanban review verdicts.

A semantic review rejection must never mark the task done or release dependent
work. The kernel, not prompt text, owns that invariant.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _review_task(conn, *, claim_review: bool = False, with_child: bool = False):
    task_id = kb.create_task(conn, title="review gate", assignee="implementer")
    child_id = None
    if with_child:
        child_id = kb.create_task(
            conn,
            title="dependent release",
            assignee="release",
            parents=[task_id],
        )
    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="implementation ready",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id) if claim_review else None
    if claim_review:
        assert review is not None
    return task_id, child_id, review


def _events(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _status(conn, task_id: str) -> str:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.status


@pytest.mark.parametrize(
    ("verdict", "expected_normalized"),
    [
        ("CONDITIONAL PASS", "CONDITIONAL PASS"),
        ("FAIL", "FAIL"),
        ("", "MISSING"),
        (None, "INVALID"),
        (7, "INVALID"),
    ],
)
def test_nonpassing_verdict_blocks_review_and_dependency_promotion(
    kanban_home: Path,
    verdict,
    expected_normalized: str,
) -> None:
    with kb.connect() as conn:
        task_id, child_id, review = _review_task(
            conn,
            claim_review=True,
            with_child=True,
        )
        assert review is not None and child_id is not None

        with pytest.raises(kb.NonPassingVerdictError):
            kb.complete_task(
                conn,
                task_id,
                summary="review result",
                metadata={"verdict": verdict},
                expected_run_id=review.current_run_id,
            )

        assert _status(conn, task_id) == "running"
        assert _status(conn, child_id) == "todo"
        events = _events(conn, task_id, "completion_blocked_nonpassing_verdict")
        assert events[-1]["verdict"] == expected_normalized
        assert events[-1]["source_status"] == "review"


def test_parked_review_requires_verdict_and_pass_promotes_child(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id, child_id, _ = _review_task(conn, with_child=True)
        assert child_id is not None

        with pytest.raises(kb.NonPassingVerdictError) as exc:
            kb.complete_task(conn, task_id, summary="looks okay")
        assert exc.value.verdict == "MISSING"
        assert _status(conn, task_id) == "review"
        assert _status(conn, child_id) == "todo"

        assert kb.complete_task(
            conn,
            task_id,
            summary="approved",
            metadata={"verdict": "  pass  ", "evidence": "tests green"},
        )
        assert _status(conn, task_id) == "done"
        assert _status(conn, child_id) == "ready"


def test_ordinary_completion_preserves_free_form_verdict_metadata(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="ordinary work", assignee="worker")
        run = kb.claim_task(conn, task_id)
        assert run is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="done",
            metadata={"verdict": "FAIL", "tests": 3},
            expected_run_id=run.current_run_id,
        )
        assert _status(conn, task_id) == "done"
        closed_run = kb.latest_run(conn, task_id)
        assert closed_run is not None
        assert closed_run.metadata == {"verdict": "FAIL", "tests": 3}


def test_stale_review_run_cannot_emit_false_verdict_event(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id, _, review = _review_task(conn, claim_review=True)
        assert review is not None and review.current_run_id is not None
        assert not kb.complete_task(
            conn,
            task_id,
            summary="stale rejection",
            metadata={"verdict": "FAIL"},
            expected_run_id=review.current_run_id + 1,
        )
        assert _events(conn, task_id, "completion_blocked_nonpassing_verdict") == []
        assert _status(conn, task_id) == "running"


def test_concurrent_review_request_is_rechecked_inside_completion_transaction(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="racy completion", assignee="worker")
        child_id = kb.create_task(
            conn,
            title="must stay gated",
            assignee="release",
            parents=[task_id],
        )
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None

        original_merge = kb._merge_completion_prose_artifacts
        injected = False

        def request_review_between_preflight_and_write(*args, **kwargs):
            nonlocal injected
            merged = original_merge(*args, **kwargs)
            if not injected:
                injected = True
                with kb.connect() as concurrent:
                    assert kb.request_review(
                        concurrent,
                        task_id,
                        summary="review landed during completion",
                        reviewer="reviewer",
                        expected_run_id=implementation.current_run_id,
                    )
            return merged

        monkeypatch.setattr(
            kb,
            "_merge_completion_prose_artifacts",
            request_review_between_preflight_and_write,
        )

        # Dashboard/manual callers do not bind expected_run_id. The final write
        # transaction must still see the new review and raise the same domain
        # error as preflight, after committing the rejection event.
        with pytest.raises(kb.NonPassingVerdictError) as exc:
            kb.complete_task(conn, task_id, summary="implementation done")
        assert exc.value.verdict == "MISSING"
        assert _status(conn, task_id) == "review"
        assert _status(conn, child_id) == "todo"
        events = _events(conn, task_id, "completion_blocked_nonpassing_verdict")
        assert events[-1]["verdict"] == "MISSING"
        assert events[-1]["source_status"] == "review"


def test_blocked_reviewer_escalation_still_requires_exact_pass(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        task_id, child_id, review = _review_task(
            conn,
            claim_review=True,
            with_child=True,
        )
        assert review is not None and child_id is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="maintainer decision required",
            kind="needs_input",
            expected_run_id=review.current_run_id,
        )
        assert _status(conn, task_id) == "blocked"

        with pytest.raises(kb.NonPassingVerdictError) as exc:
            kb.complete_task(conn, task_id, summary="escalation resolved")
        assert exc.value.verdict == "MISSING"
        assert _status(conn, task_id) == "blocked"
        assert _status(conn, child_id) == "todo"

        assert kb.complete_task(
            conn,
            task_id,
            summary="approved after escalation",
            metadata={"verdict": "PASS"},
        )
        assert _status(conn, task_id) == "done"
        assert _status(conn, child_id) == "ready"


def test_cli_maps_verdict_rejection_without_traceback(
    kanban_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.kanban import _cmd_complete

    with kb.connect() as conn:
        task_id, _, _ = _review_task(conn)

    rc = _cmd_complete(
        argparse.Namespace(
            task_ids=[task_id],
            summary="conditional",
            result=None,
            metadata=json.dumps({"verdict": "CONDITIONAL PASS"}),
        )
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "metadata.verdict" in captured.err
    assert "request changes" in captured.err
    assert "Traceback" not in captured.err
    with kb.connect() as conn:
        assert _status(conn, task_id) == "review"
