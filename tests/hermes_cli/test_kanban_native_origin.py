"""Native source obligations coexist with install telemetry; no live transport."""
import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    from hermes_cli import config
    policy = {"kanban": {"fixed_notify_target": {
        "enabled": True, "platform": "telegram", "chat_id": "fixture-chat",
        "thread_id": "101", "notifier_profile": "default", "chat_type": "group",
        "delivery_mode": "notify+wake",
        "board_thread_overrides": {"board-alpha": "102", "board-beta": "104"},
        "board_wake_thread_overrides": {"board-split": "103"},
    }}}
    monkeypatch.setattr(config, "load_config", lambda: policy)
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def source(topic="201"):
    return dict(platform="telegram", chat_id="fixture-chat", thread_id=topic,
                chat_type="group", user_id="fixture-user", notifier_profile="default",
                delivery_mode="notify+wake", delivery_metadata={
                    "kanban_source": True, "kanban_origin_session_id": "fixture-origin"})


def route(topic) -> dict[str, Any]:
    return dict(platform="telegram", chat_id="fixture-chat", thread_id=topic)


@pytest.mark.parametrize("topic", ["201", "202"])
@pytest.mark.parametrize("board,telemetry", [
    ("board-unlisted", {"101"}), ("board-alpha", {"102"}), ("board-beta", {"104"}),
    ("board-split", {"101", "103"}),
])
def test_atomic_source_and_policy_reconciliation(conn, topic, board, telemetry):
    tid = kb.create_task(conn, title="synthetic", board=board, source_notify_target=source(topic))
    subs = notify.list_notify_subs(conn, tid)
    assert {s["thread_id"] for s in subs} == telemetry | {topic}
    origin = next(s for s in subs if s["thread_id"] == topic)
    assert origin["user_id"] == "fixture-user"
    assert origin["notifier_profile"] == "default"
    assert origin["delivery_mode"] == "notify+wake"
    notify.add_notify_sub(conn, task_id=tid, board=board, **route("101"))
    assert next(s for s in notify.list_notify_subs(conn, tid) if s["thread_id"] == topic) == origin


def test_no_origin_cli_and_duplicate_do_not_guess_or_rebind(conn):
    tid = kb.create_task(conn, title="CLI", board="board-unlisted")
    assert {s["thread_id"] for s in notify.list_notify_subs(conn, tid)} == {"101"}
    original = kb.create_task(conn, title="first", board="board-unlisted", idempotency_key="fixture-key",
                              source_notify_target=source())
    duplicate = kb.create_task(conn, title="retry", board="board-unlisted", idempotency_key="fixture-key",
                               source_notify_target=source("202"))
    assert duplicate == original
    assert {s["thread_id"] for s in notify.list_notify_subs(conn, original)} == {"201", "101"}


def test_origin_on_telemetry_topic_retains_identity(conn):
    tid = kb.create_task(conn, title="same route", board="board-unlisted", source_notify_target=source("101"))
    subs = notify.list_notify_subs(conn, tid)
    assert len(subs) == 1
    assert subs[0]["user_id"] == "fixture-user"
    assert subs[0]["delivery_metadata"]["kanban_source"] is True


def test_child_inherits_default_source(conn):
    parent = kb.create_task(conn, title="creator", board="board-unlisted", session_id="fixture-origin",
                            source_notify_target=source())
    with kb.write_txn(conn):
        kb._append_event(conn, parent, "completed", {"summary": "synthetic"})
    notify.claim_unseen_events_for_sub(conn, task_id=parent, **route("201"))
    child = kb.create_task(conn, title="worker child", board="board-unlisted", creator_task_id=parent)
    task = kb.get_task(conn, child)
    assert task is not None and task.session_id == "fixture-origin"
    origin = next(s for s in notify.list_notify_subs(conn, child) if s["thread_id"] == "201")
    assert origin["notifier_profile"] == "default"
    assert origin["delivery_metadata"]["kanban_source"] is True


@pytest.mark.parametrize("success", [True, False, None])
def test_strict_artifact_delivery_only_explicit_intended_paths(tmp_path, monkeypatch, success):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from gateway.platforms.base import BasePlatformAdapter
    intended = tmp_path / "intended.md"
    internal = tmp_path / "internal.txt"
    intended.write_text("synthetic human deliverable")
    internal.write_text("synthetic internal evidence")
    monkeypatch.setattr(BasePlatformAdapter, "filter_local_delivery_paths", staticmethod(lambda p: p))
    adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=success)),
                              extract_local_files=lambda text: ([str(internal)], []))
    kwargs: dict[str, Any] = dict(adapter=adapter, chat_id="fixture-chat", metadata={"thread_id": "201"},
                  event_payload={"artifacts": [str(intended)], "summary": str(internal)},
                  task=SimpleNamespace(result=str(internal)), strict=True)
    failures = asyncio.run(GatewayKanbanWatchersMixin()._deliver_kanban_artifacts(**kwargs))
    assert failures == ([] if success is True else ["intended.md: upload not acknowledged"])
    adapter.send_document.assert_awaited_once_with(chat_id="fixture-chat", file_path=str(intended), metadata={"thread_id": "201"})


def test_definitely_deferred_claim_can_retry_without_losing_origin(conn):
    tid = kb.create_task(conn, title="deferred", board="board-unlisted", source_notify_target=source())
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "completed", {"summary": "synthetic"})
    old, claimed, events = notify.claim_unseen_events_for_sub(conn, task_id=tid, **route("201"))
    assert notify.rewind_notify_cursor(conn, task_id=tid, claimed_cursor=claimed, old_cursor=old, **route("201"))
    assert notify.claim_unseen_events_for_sub(conn, task_id=tid, **route("201"))[2] == events


def test_reconciler_does_not_delete_native_origin(conn):
    tid = kb.create_task(conn, title="active", board="board-unlisted",
                         source_notify_target=source())
    before = next(s for s in notify.list_notify_subs(conn, tid)
                  if s["delivery_metadata"].get("kanban_source"))
    # A minimal install-owned telemetry policy uses the same DB seam as the
    # normal subscription reconciler, without any deployment-specific script.
    telemetry = dict(platform="telegram", chat_id="fixture-chat", thread_id="101",
                     notifier_profile="default", delivery_mode="notify+wake")
    with kb.write_txn(conn):
        notify._replace_fixed_notify_subs(conn, task_id=tid, targets=[telemetry])
    after = notify.list_notify_subs(conn, tid)
    assert next(s for s in after if s["delivery_metadata"].get("kanban_source")) == before
    assert {s["thread_id"] for s in after} == {"101", "201"}


def test_strict_source_artifact_policy_denial_is_not_success(tmp_path, monkeypatch):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from gateway.platforms.base import BasePlatformAdapter
    intended = tmp_path / "intended.md"
    intended.write_text("synthetic")
    monkeypatch.setattr(BasePlatformAdapter, "filter_local_delivery_paths", staticmethod(lambda p: []))
    adapter = SimpleNamespace(send_document=AsyncMock())
    failures = asyncio.run(GatewayKanbanWatchersMixin()._deliver_kanban_artifacts(
        adapter=adapter, chat_id="fixture-chat", metadata={}, task=None,
        event_payload={"artifacts": [str(intended), str(tmp_path / "gone.md")]}, strict=True))
    assert failures == ["gone.md: missing", "intended.md: denied by delivery policy"]
    adapter.send_document.assert_not_awaited()
