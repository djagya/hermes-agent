"""Legacy acceptance is explicit; recovery is compare-and-swap, never a rewrite."""

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_recovery import recovery_snapshot, recover_triage_task


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as connection:
        yield connection


def _escalated(conn):
    tid = kb.create_task(conn, title="Accepted work", body="  exact\nbytes\n", assignee="worker")
    kb.recompute_ready(conn)
    for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
        if attempt:
            assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="Resolved obstacle", kind="capability")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def _arguments(snapshot):
    return dict(expected_fingerprint=snapshot["fingerprint"], author="operator",
                reason="Reviewed legacy specification and resolved obstacle",
                role="executable", legacy_acceptance=True,
                resolved_blocker=snapshot["blocker"])


@pytest.mark.parametrize("graph", ["root", "children", "waiting-parent"])
def test_recovery_preserves_content_graph_and_native_readiness(conn, graph):
    tid = _escalated(conn)
    if graph == "children":
        child = kb.create_task(conn, title="Consumer", assignee="worker")
        kb.link_tasks(conn, parent_id=tid, child_id=child)
    if graph == "waiting-parent":
        parent = kb.create_task(conn, title="Prerequisite", assignee="worker", triage=True)
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
    before = recovery_snapshot(conn, tid)
    comments = list(conn.execute("SELECT * FROM task_comments"))
    assert recover_triage_task(conn, tid, **_arguments(before))
    after = recovery_snapshot(conn, tid)
    assert after["task"] == {**before["task"], "status": "todo"}
    assert after["parents"] == before["parents"]
    assert after["children"] == before["children"]
    assert after["runs"] == before["runs"]
    assert list(conn.execute("SELECT * FROM task_comments")) == comments
    assert after["events"][:-1] == before["events"]
    audit = after["events"][-1]
    assert audit["kind"] == "triage_recovered"
    payload = json.loads(audit["payload"])
    assert payload["before_fingerprint"] == before["fingerprint"]
    assert payload["resolved_blocker"] == before["blocker"]
    assert payload["legacy_acceptance"] is True
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == ("todo" if graph == "waiting-parent" else "ready")


@pytest.mark.parametrize("case", [
    "spec", "config", "dependency", "blocker", "claim", "worker", "run-pointer",
    "live-run", "human-hold", "human-event", "missing-blocker", "wrong-blocker",
    "role", "attestation", "reason", "delegated", "audit-failure",
])
def test_recovery_refusal_and_audit_failure_leave_database_unchanged(conn, monkeypatch, case):
    tid = _escalated(conn)
    snapshot = recovery_snapshot(conn, tid)
    stale = case in {"spec", "config", "dependency", "blocker"}
    # A separate native connection can commit between operator review and recovery.
    with kbc.connect_closing() as writer, kb.write_txn(writer):
        updates = {
            "spec": "body = 'changed'", "config": "priority = priority + 1",
            "claim": "claim_lock = 'owner'", "worker": "worker_pid = 123",
            "run-pointer": "current_run_id = 999", "human-hold": "block_kind = 'needs_input'",
        }
        if case in updates:
            writer.execute(f"UPDATE tasks SET {updates[case]} WHERE id = ?", (tid,))
        if case == "dependency":
            writer.execute("INSERT INTO task_links VALUES (?, ?)", ("missing-parent", tid))
        if case in {"blocker", "human-event"}:
            kb._append_event(writer, tid, "blocked", {"kind": "needs_input" if case == "human-event" else "capability"})
        if case == "missing-blocker":
            writer.execute("DELETE FROM task_events WHERE task_id = ?", (tid,))
        if case == "live-run":
            writer.execute("UPDATE task_runs SET ended_at = NULL WHERE task_id = ?", (tid,))
    if not stale:
        snapshot = recovery_snapshot(conn, tid)
    args = _arguments(snapshot)
    changes = {"role": ("role", "human-gate"), "attestation": ("legacy_acceptance", False),
               "reason": ("reason", " "), "wrong-blocker": ("resolved_blocker", {"id": -1})}
    if case in changes:
        key, value = changes[case]
        args[key] = value
    error = None
    if case == "delegated":
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
        error = PermissionError
    elif case == "audit-failure":
        def fail(*args, **kwargs):
            raise RuntimeError("audit unavailable")
        monkeypatch.setattr(kb, "_append_event", fail)
        error = RuntimeError
    elif case in {"role", "attestation", "reason", "missing-blocker"}:
        error = ValueError
    before = list(conn.iterdump())
    if error:
        with pytest.raises(error):
            recover_triage_task(conn, tid, **args)
    else:
        assert not recover_triage_task(conn, tid, **args)
    assert list(conn.iterdump()) == before
