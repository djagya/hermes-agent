"""The public registered block tool forwards stable cause identity to storage."""

import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_registered_block_tool_preserves_cause_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbc.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Tool cause identity", assignee="worker")
        assert kb.claim_task(conn, tid) is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    from tools import kanban_tools  # noqa: F401 — register the real handler
    from tools.registry import registry

    result = json.loads(registry.dispatch("kanban_block", {
        "reason": "Synthetic source packet unavailable", "kind": "capability",
        "blocker_key": "source-packet-unreadable",
    }))
    assert result["ok"] is True
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.blocker_key == "source-packet-unreadable"
        assert kb.list_events(conn, tid)[-1].payload["blocker_key"] == task.blocker_key
