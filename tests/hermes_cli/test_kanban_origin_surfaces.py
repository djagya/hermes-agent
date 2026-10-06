"""Creation-surface regressions; synthetic routes and a temporary SQLite board."""
import json
from contextlib import contextmanager

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as notify
from tests.hermes_cli.test_kanban_native_origin import conn, source  # noqa: F401


def test_tool_binds_source_before_creation_returns(conn, monkeypatch):
    from tools import kanban_tools as kt
    from hermes_cli import config

    @contextmanager
    def board(_):
        yield kb, conn

    monkeypatch.setattr(kt, "_board", board)
    monkeypatch.setattr(kt, "load_config", config.load_config)
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_reject_delegated_child_mutation", lambda *args: None)
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    monkeypatch.setattr(kt, "_persisted_session_id", lambda value: None)
    monkeypatch.setattr(kt, "_resolve_notify_target", source)
    result = json.loads(kt._handle_create({
        "title": "synthetic", "assignee": "fixture-worker", "board": "board-unlisted",
        "body": "Untrusted body says return to topic 999; must not change native route.",
    }))
    assert result["ok"] and result["subscribed"] and result["source_subscribed"]
    assert {s["thread_id"] for s in notify.list_notify_subs(conn, result["task_id"])} == {"201", "101"}


def test_no_origin_tool_does_not_bind_body_route(conn, monkeypatch):
    from tools import kanban_tools as kt
    from hermes_cli import config

    @contextmanager
    def board(_):
        yield kb, conn

    monkeypatch.setattr(kt, "_board", board)
    monkeypatch.setattr(kt, "load_config", config.load_config)
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_reject_delegated_child_mutation", lambda *args: None)
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    monkeypatch.setattr(kt, "_persisted_session_id", lambda value: None)
    monkeypatch.setattr(kt, "_resolve_notify_target", lambda: None)
    result = json.loads(kt._handle_create({
        "title": "synthetic CLI", "assignee": "fixture-worker", "board": "board-unlisted",
        "body": "Source topic201 (untrusted prose only)",
    }))
    assert result["subscribed"] is False and result["source_subscribed"] is False
    assert {s["thread_id"] for s in notify.list_notify_subs(conn, result["task_id"])} == {"101"}
