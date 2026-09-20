"""Cause identity survives unblock; absent keys retain legacy accounting."""

from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as connection:
        yield connection


@pytest.mark.parametrize("keys", [("packet-unreadable", "spillover-unreadable"),
                                  ("packet-unreadable", "packet-unreadable"),
                                  (None, None), ("packet-unreadable", None),
                                  (None, "packet-unreadable")])
def test_cause_count_and_cli_forwarding(conn, keys):
    tid = kb.create_task(conn, title="Bounded work", assignee="worker")
    for index, key in enumerate(keys):
        if index:
            assert kb.unblock_task(conn, tid)
        option = f" --blocker-key {key}" if key is not None else ""
        cli.run_slash(f"block {tid} --kind capability{option}")
        task = kb.get_task(conn, tid)
        assert task.blocker_key == key
    same = keys[1] is None or keys[0] == keys[1]
    assert task.block_recurrences == (2 if same else 1)
    assert task.status == ("triage" if same else "blocked")
    event = kb.list_events(conn, tid)[-1]
    assert event.payload.get("blocker_key") == keys[-1]
    if keys[-1] is None:
        assert "blocker_key" not in event.payload


@pytest.mark.parametrize("case", ["invalid-empty", "invalid-long", "dependency", "review", "completion", "migration"])
def test_key_does_not_weaken_other_lifecycle_contracts(conn, case):
    tid = kb.create_task(conn, title="Bounded work", assignee="worker")
    if case.startswith("invalid"):
        before = list(conn.iterdump())
        with pytest.raises(ValueError):
            kb.block_task(conn, tid, kind="capability", blocker_key=" " if case == "invalid-empty" else "x" * 129)
        assert list(conn.iterdump()) == before
        return
    if case == "migration":
        # Reopen a pre-key board: no fabricated identities or historical rewrites.
        with kb.write_txn(conn):
            conn.execute("ALTER TABLE tasks DROP COLUMN blocker_key")
        kbc._INITIALIZED_PATHS.clear()
        with kbc.connect_closing() as reopened:
            assert kb.get_task(reopened, tid).blocker_key is None
        return
    assert kb.block_task(conn, tid, kind="capability", blocker_key="packet-unreadable")
    assert kb.unblock_task(conn, tid)
    if case == "dependency":
        # Discover the prerequisite during a run; linking it while ready would
        # already demote the task to todo, where block_task must refuse it.
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        parent = kb.create_task(conn, title="Prerequisite", triage=True)
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "running"
        assert kb.block_task(conn, tid, kind="dependency", blocker_key="prerequisite")
        kb.recompute_ready(conn)
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "todo"
        assert task.block_recurrences == 1
        assert task.blocker_key == "prerequisite"
        event = kb.list_events(conn, tid)[-1]
        assert event.kind == "dependency_wait"
        assert event.payload is not None and event.payload["blocker_key"] == "prerequisite"
        before = list(conn.iterdump())
        assert not kb.block_task(conn, tid, kind="dependency", blocker_key="prerequisite")
        assert list(conn.iterdump()) == before
    elif case == "review":
        assert kb.request_review(conn, tid, summary="Candidate", reviewer="reviewer")
        assert kb.claim_review_task(conn, tid, claimer="reviewer") is not None
        ok, implementer = kb.request_changes(conn, tid, reason="Correct implementation")
        assert ok and implementer == "worker"
        assert kb.get_task(conn, tid).blocker_key == "packet-unreadable"
        assert kb.get_task(conn, tid).block_recurrences == 1
    else:
        assert kb.complete_task(conn, tid, result="Completed")
        assert kb.get_task(conn, tid).blocker_key is None
        assert kb.get_task(conn, tid).block_recurrences == 0
