"""Operator decisions are exact-event CAS operations, not generic force transitions."""

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_db_operator import complete_triage_gate, resolve_triage_blocker
from hermes_cli.kanban_db_recovery import recovery_snapshot, recover_triage_task
from hermes_cli.kanban_parser import build_parser


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                 "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    with kbc.connect_closing() as connection:
        yield connection


def _common(snapshot):
    return dict(expected_fingerprint=snapshot["fingerprint"], author="operator",
                reason="Reviewed exact fixture evidence", operator=True, process_quiescent=True)


def _gate_args(snapshot) -> dict:
    return dict(**_common(snapshot), role="operator-evidence-gate", nonspawning=True,
                summary="Fixture evidence accepted",
                metadata={"evidence": {"reference": "fixture://independent-receipt"}})


def _held(conn, *, review=False):
    tid = kb.create_task(conn, title="Executable fixture", body="  exact\nbytes\n", assignee="builder")
    kb.recompute_ready(conn)
    if review:
        assert kb.request_review(conn, tid, summary="Ready", reviewer="reviewer")
    for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
        if attempt:
            assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, kind="needs_input", blocker_key="fixture-decision",
                             reason="Explicit technical decision required")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def _resolution_args(snapshot) -> dict:
    return dict(**_common(snapshot), role="executable", resolved_blocker=snapshot["blocker"],
                decision="Operator explicitly resolves only this technical hold",
                evidence_reference="fixture://decision-record")


def _set(conn, tid, assignment):
    with kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {assignment} WHERE id = ?", (tid,))


@pytest.mark.parametrize("case", [
    "success", "parent", "evidence", "summary", "reason", "role", "nonspawning",
    "operator", "quiescent", "claim", "expiry", "worker", "run-pointer", "live-run",
    "stale", "parent-race", "audit-failure", "worker-context", "child-context",
    "typed-hold", "typed-event", "goal", "contract-pass", "contract-fail", "artifact", "missing-artifact",
])
def test_gate_closure_is_atomic_evidenced_and_never_runnable(conn, monkeypatch, case):
    parent = kb.create_task(conn, title="Prerequisite", assignee="builder")
    kb.recompute_ready(conn)
    if case != "parent":
        assert kb.complete_task(conn, parent, summary="Fixture prerequisite met")
    tid = kb.create_task(conn, title="Evidence barrier", body="preserve this spec", assignee="operator", triage=True)
    child = kb.create_task(conn, title="Consumer", assignee="builder")
    kb.link_tasks(conn, parent_id=parent, child_id=tid)
    kb.link_tasks(conn, parent_id=tid, child_id=child)
    kb.add_comment(conn, tid, "operator", "Historical evidence remains")
    ordinary_before = list(conn.iterdump())
    assert not kb.complete_task(conn, tid, summary="Ordinary completion must still refuse triage")
    assert list(conn.iterdump()) == ordinary_before
    artifact = None
    if case in {"artifact", "missing-artifact"}:
        ws = kbw.resolve_workspace(kb.get_task(conn, tid))
        kbw.set_workspace_path(conn, tid, ws)
        artifact = ws / "receipt.txt"
        if case == "artifact":
            artifact.write_text("synthetic deliverable", encoding="utf-8")
    updates = {
        "claim": "claim_lock = 'owner'", "expiry": "claim_expires = 1",
        "worker": "worker_pid = 123", "run-pointer": "current_run_id = 999",
        "typed-hold": "block_kind = 'needs_input'", "goal": "goal_mode = 1",
    }
    if case in updates:
        _set(conn, tid, updates[case])
    if case == "typed-event":
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "blocked", {"kind": "needs_input"})
    if case == "live-run":
        with kb.write_txn(conn):
            run_id = kb._synthesize_ended_run(conn, tid, outcome="blocked", summary="old run")
            conn.execute("UPDATE task_runs SET ended_at = NULL WHERE id = ?", (run_id,))
    if case.startswith("contract-") or case == "parent-race":
        _set(conn, tid, "completion_contract = 'example/repo'")
        from hermes_cli import kanban_pr_acceptance as acceptance
        original_collect = acceptance.collect_acceptance

        def collect(contract, published_pr):
            assert contract == "example/repo"
            assert published_pr == "https://github.com/example/repo/pull/1"
            if case == "parent-race":
                with kbc.connect_closing() as writer:
                    _set(writer, parent, "status = 'triage'")
            return {"ok": case != "contract-fail", "pr_url": published_pr,
                    "classification": "success" if case != "contract-fail" else "missing",
                    "head_sha": "a" * 40, "checks": [], "recovery": "Fixture retry instruction"}

        monkeypatch.setattr(acceptance, "collect_acceptance", collect)
    snapshot = recovery_snapshot(conn, tid)
    args = _gate_args(snapshot)
    if case.startswith("contract-") or case == "parent-race":
        args["metadata"]["published_pr"] = "https://github.com/example/repo/pull/1"
    if artifact:
        args["metadata"]["artifacts"] = [str(artifact)]
    bad_args = {"evidence": ("metadata", {}), "summary": ("summary", " "),
                "reason": ("reason", " "), "role": ("role", "executable"),
                "nonspawning": ("nonspawning", False), "operator": ("operator", False),
                "quiescent": ("process_quiescent", False)}
    if case in bad_args:
        key, value = bad_args[case]
        args[key] = value
    if case == "stale":
        with kbc.connect_closing() as writer:
            _set(writer, tid, "body = 'changed by another operator'")
    if case == "audit-failure":
        original = kb._append_event

        def fail(conn, task_id, kind, *a, **kw):
            if kind == "operator_gate_completed":
                raise RuntimeError("audit unavailable")
            return original(conn, task_id, kind, *a, **kw)

        monkeypatch.setattr(kb, "_append_event", fail)
    context = {"worker-context": "HERMES_KANBAN_TASK", "child-context": "HERMES_DELEGATED_CHILD_CONTEXT"}
    if case in context:
        monkeypatch.setenv(context[case], "fixture")
    # A DB trigger observes every status write, not only the state after commit.
    conn.executescript("""
        CREATE TEMP TABLE transitions(task_id TEXT, old_status TEXT, new_status TEXT);
        CREATE TEMP TRIGGER record_transition AFTER UPDATE OF status ON tasks
        WHEN OLD.status != NEW.status BEGIN
            INSERT INTO transitions VALUES (NEW.id, OLD.status, NEW.status);
        END;
    """)
    before = list(conn.iterdump())
    error = (ValueError if case in bad_args else PermissionError if case in context
             else RuntimeError if case in {"audit-failure", "missing-artifact"} else None)
    success = case in {"success", "contract-pass", "artifact"}
    if error:
        with pytest.raises(error):
            complete_triage_gate(conn, tid, **args)
    else:
        assert complete_triage_gate(conn, tid, **args) is success
    if not success:
        assert kb.get_task(conn, tid).status == "triage"
        assert not list(conn.execute("SELECT * FROM transitions WHERE task_id = ?", (tid,)))
        if case not in {"contract-fail", "parent-race"}:
            assert list(conn.iterdump()) == before
        if case == "contract-fail":
            assert kb.get_task(conn, tid).completion_contract == args["metadata"]["published_pr"]
            assert kb.get_task(conn, tid).last_failure_error
            monkeypatch.setattr(acceptance, "collect_acceptance", original_collect)

            def no_network(*args, **kwargs):
                raise AssertionError("mismatched PR must fail before network I/O")

            monkeypatch.setattr(acceptance, "_api", no_network)
            retry_args = _gate_args(recovery_snapshot(conn, tid))
            retry_args["metadata"]["published_pr"] = "https://github.com/example/repo/pull/2"
            assert not complete_triage_gate(conn, tid, **retry_args)
            assert kb.get_task(conn, tid).completion_contract == args["metadata"]["published_pr"]
        return
    transitions = [tuple(row) for row in conn.execute(
        "SELECT old_status, new_status FROM transitions WHERE task_id = ?", (tid,))]
    assert transitions == [("triage", "done")]
    after = recovery_snapshot(conn, tid)
    for field in ("title", "body", "assignee", "workspace_kind", "workspace_path"):
        assert after["task"][field] == snapshot["task"][field]
    assert kb.parent_ids(conn, tid) == [parent]
    assert kb.child_ids(conn, tid) == [child]
    assert kb.get_task(conn, child).status == "ready"
    assert after["events"][:len(snapshot["events"])] == snapshot["events"]
    assert kb.latest_run(conn, tid).metadata["evidence"] == args["metadata"]["evidence"]
    if artifact:
        stored = kb.list_attachments(conn, tid)
        assert len(stored) == 1
        assert Path(stored[0].stored_path).read_text() == "synthetic deliverable"
    before_replay = list(conn.iterdump())
    assert not complete_triage_gate(conn, tid, **args)
    assert list(conn.iterdump()) == before_replay


@pytest.mark.parametrize("case", [
    "success", "review", "waiting-review", "waiting-parent", "no-decision", "no-evidence",
    "wrong-event", "old-event", "stale", "different-hold", "claim", "live-run",
    "role", "operator", "quiescent", "worker-context", "audit-failure",
])
def test_exact_typed_resolution_preserves_history_and_parent_review_routing(conn, monkeypatch, case):
    tid = _held(conn, review=case in {"review", "waiting-review"})
    child = kb.create_task(conn, title="Descendant", assignee="builder")
    kb.link_tasks(conn, parent_id=tid, child_id=child)
    parent = None
    if case in {"waiting-parent", "waiting-review"}:
        parent = kb.create_task(conn, title="Unfinished prerequisite", assignee="builder")
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
    snapshot = recovery_snapshot(conn, tid)
    before = list(conn.iterdump())
    assert not recover_triage_task(
        conn, tid, expected_fingerprint=snapshot["fingerprint"], author="operator",
        reason="Technical text is not approval", role="executable", legacy_acceptance=True,
        resolved_blocker=snapshot["blocker"],
    )
    assert list(conn.iterdump()) == before
    updates = {"different-hold": "blocker_key = 'another-decision'", "claim": "claim_lock = 'owner'"}
    if case in updates:
        _set(conn, tid, updates[case])
        snapshot = recovery_snapshot(conn, tid)
    if case == "live-run":
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET ended_at = NULL WHERE task_id = ?", (tid,))
        snapshot = recovery_snapshot(conn, tid)
    args = _resolution_args(snapshot)
    bad_args = {"no-decision": ("decision", ""), "no-evidence": ("evidence_reference", " "),
                "role": ("role", "operator-evidence-gate"), "operator": ("operator", False),
                "quiescent": ("process_quiescent", False)}
    if case in bad_args:
        key, value = bad_args[case]
        args[key] = value
    if case == "wrong-event":
        args["resolved_blocker"] = {**snapshot["blocker"], "id": -1}
    if case == "old-event":
        args["resolved_blocker"] = next(e for e in snapshot["events"] if e["kind"] == "blocked")
    if case == "stale":
        with kbc.connect_closing() as writer:
            kb.add_comment(writer, tid, "another-operator", "New evidence invalidates snapshot")
    if case == "worker-context":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "fixture-worker")
    if case == "audit-failure":
        def fail(*args, **kwargs):
            raise RuntimeError("audit unavailable")
        monkeypatch.setattr(kb, "_append_event", fail)
    before = list(conn.iterdump())
    error = (ValueError if case in bad_args else PermissionError if case == "worker-context"
             else RuntimeError if case == "audit-failure" else None)
    success = case in {"success", "review", "waiting-parent", "waiting-review"}
    if error:
        with pytest.raises(error):
            resolve_triage_blocker(conn, tid, **args)
    else:
        assert resolve_triage_blocker(conn, tid, **args) is success
    if not success:
        assert list(conn.iterdump()) == before
        return
    after = recovery_snapshot(conn, tid)
    assert after["task"] == {**snapshot["task"], "status": "todo",
                             "consecutive_failures": 0, "last_failure_error": None}
    assert after["events"][:-1] == snapshot["events"]
    assert after["runs"] == snapshot["runs"]
    assert after["children"] == snapshot["children"]
    payload = json.loads(after["events"][-1]["payload"])
    assert payload["resolved_blocker"] == snapshot["blocker"]
    assert payload["decision"] == args["decision"]
    assert payload["evidence_reference"] == args["evidence_reference"]
    before_replay = list(conn.iterdump())
    assert not resolve_triage_blocker(conn, tid, **args)
    fresh_args = _resolution_args(after)
    assert not resolve_triage_blocker(conn, tid, **fresh_args)
    assert list(conn.iterdump()) == before_replay
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == ("todo" if parent else "review" if case == "review" else "ready")
    if parent:
        assert kb.complete_task(conn, parent, summary="Fixture prerequisite met")
        assert kb.get_task(conn, tid).status == ("review" if case == "waiting-review" else "ready")
    assert kb.get_task(conn, tid).assignee == ("reviewer" if "review" in case else "builder")
    assert kb.get_task(conn, child).status == "todo"
    # Even a fresh fingerprint must not resolve the same event twice if later
    # administration returns the task to triage without a new blocking event.
    _set(conn, tid, "status = 'triage'")
    replay = _resolution_args(recovery_snapshot(conn, tid))
    before_replay = list(conn.iterdump())
    assert not resolve_triage_blocker(conn, tid, **replay)
    assert list(conn.iterdump()) == before_replay


@pytest.mark.parametrize("operation", ["complete-gate", "resolve-blocker"])
def test_cli_consumes_exact_operator_request_on_explicit_board(conn, tmp_path, capsys, operation):
    tid = (kb.create_task(conn, title="Operator barrier", assignee="operator", triage=True)
           if operation == "complete-gate" else _held(conn))
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))

    def invoke(*args):
        return cli.kanban_command(parser.parse_args(["kanban", *args]))

    assert invoke("operator-recover", "snapshot", tid) != 0
    capsys.readouterr()
    assert invoke("--board", "default", "operator-recover", "snapshot", tid) == 0
    snapshot = json.loads(capsys.readouterr().out)
    request = _gate_args(snapshot) if operation == "complete-gate" else _resolution_args(snapshot)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert invoke("--board", "default", "operator-recover", operation, tid, "--request", str(path)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == ("done" if operation == "complete-gate" else "todo")
    assert kb.get_task(conn, tid).status == result["status"]
    assert invoke("--board", "default", "operator-recover", operation, tid, "--request", str(path)) != 0
