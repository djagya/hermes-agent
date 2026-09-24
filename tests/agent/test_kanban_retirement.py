"""Run-scoped retirement of dispatcher-spawned Kanban workers.

A committed handoff ends the worker's pinned run; the worker must not dispatch any
further tool — including a trailing writer in the same batch — while a failed lifecycle
call keeps the run (and repair guidance) alive.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from run_agent import AIAgent


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """A real board with one claimed task; this process is its pinned worker."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="t", assignee="builder")
        run_id = kb.claim_task(conn, task_id).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return SimpleNamespace(task_id=task_id, run_id=run_id)


def _make_agent() -> AIAgent:
    tool_defs = [
        {"type": "function", "function": {"name": n, "description": n,
                                          "parameters": {"type": "object", "properties": {}}}}
        for n in ("kanban_request_review", "write_file", "terminal")
    ]
    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("hermes_cli.config.load_config_readonly", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True, platform="cli",
        )
    agent.client = MagicMock()
    agent.save_trajectories = False
    return agent


def _call(name, args, call_id):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}", type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _run_batch(agent, worker, *, handoff_succeeds: bool, concurrent: bool = False):
    """``kanban_request_review`` followed by a trailing ``write_file`` in ONE batch."""
    executed: list[str] = []

    def fake_handle(name, args, task_id, **kwargs):
        executed.append(name)
        if name == "kanban_request_review":
            if not handoff_succeeds:
                return json.dumps({"error": "artifact preservation failed"})
            with kbc.connect_closing() as conn:
                assert kb.request_review(conn, worker.task_id, summary="done",
                                         expected_run_id=worker.run_id)
            return json.dumps({"ok": True})
        return json.dumps({"ok": name})

    calls = [
        _call("kanban_request_review", {"summary": "done"}, "c-handoff"),
        _call("write_file", {"path": "/tmp/x", "content": "late"}, "c-trailing"),
    ]
    messages: list[dict] = []
    msg = SimpleNamespace(content="", tool_calls=calls)
    with patch("model_tools.handle_function_call", side_effect=fake_handle):
        run = agent._execute_tool_calls_concurrent if concurrent else agent._execute_tool_calls_sequential
        run(msg, messages, "task-1")
    return executed, messages


def test_successful_handoff_refuses_trailing_writer_and_keeps_pairing(worker):
    executed, messages = _run_batch(_make_agent(), worker, handoff_succeeds=True)

    assert executed == ["kanban_request_review"]
    # Every tool_call still has its paired result, in emission order.
    assert [m["tool_call_id"] for m in messages] == ["c-handoff", "c-trailing"]
    assert "kanban_run_retired" in messages[1]["content"]


def test_successful_handoff_refuses_trailing_writer_on_concurrent_path(worker):
    executed, messages = _run_batch(_make_agent(), worker, handoff_succeeds=True, concurrent=True)

    assert "write_file" not in executed
    assert [m["tool_call_id"] for m in messages] == ["c-handoff", "c-trailing"]
    assert "kanban_run_retired" in messages[1]["content"]


def test_failed_handoff_keeps_worker_live(worker):
    executed, messages = _run_batch(_make_agent(), worker, handoff_succeeds=False)

    assert executed == ["kanban_request_review", "write_file"]
    assert "kanban_run_retired" not in messages[1]["content"]


def test_successor_run_retires_the_predecessor_worker(worker):
    """A reclaimed run whose task is already running again under a successor stays
    retired: the successor's ``running`` must not revive it."""
    from agent.kanban_retirement import admission_block

    with kbc.connect_closing() as conn:
        assert kb.block_task(conn, worker.task_id, reason="x", expected_run_id=worker.run_id)
        assert kb.unblock_task(conn, worker.task_id)
        successor = kb.claim_task(conn, worker.task_id)
    assert successor is not None and successor.current_run_id != worker.run_id

    refusal = admission_block(SimpleNamespace(), "terminal")
    assert refusal is not None and "kanban_run_retired" in refusal


def test_unknown_ownership_refuses_writers_but_not_reads_or_lifecycle(worker, monkeypatch):
    from agent import kanban_retirement as kr

    monkeypatch.setattr(kr, "read_run_state",
                        lambda t, r: kr.RunState("unknown", t, r, detail="database is locked"))
    agent = SimpleNamespace()
    assert "kanban_run_ownership_unknown" in kr.admission_block(agent, "write_file")
    assert kr.admission_block(agent, "read_file") is None
    assert kr.admission_block(agent, "kanban_block") is None


def test_no_pinned_run_leaves_ordinary_sessions_untouched(monkeypatch):
    from agent.kanban_retirement import admission_block

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert admission_block(SimpleNamespace(), "write_file") is None


def test_stop_nudge_follows_committed_run_state_not_tool_names(worker):
    """Successful review handoff: no false complete/block nudge. Failed terminal call in
    history with the run still live: repair nudge stays."""
    from agent.turn_stop_gates import _kanban_stop_nudge

    failed_history = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "kanban_complete", "arguments": "{}"}}]}]
    assert _kanban_stop_nudge(SimpleNamespace(), failed_history) is not None

    with kbc.connect_closing() as conn:
        assert kb.request_review(conn, worker.task_id, summary="done", expected_run_id=worker.run_id)
    assert _kanban_stop_nudge(SimpleNamespace(), []) is None
