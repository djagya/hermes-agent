"""Native-origin notifier delivery against a real adapter wire and SQLite board."""
import asyncio

import pytest

from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import Platform, PlatformConfig
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


def _route():
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.adapters = {adapter.platform: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._kanban_dispatcher_lock_handle = object()
    source = SessionSource(platform=adapter.platform, chat_id="42", user_id="42", chat_type="dm")
    return runner, adapter, build_session_key(source)


def _source_task(key, artifacts):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="origin task", assignee="worker", source_notify_target=dict(
            platform="telegram", chat_id="42", user_id="42", chat_type="dm",
            delivery_mode="notify+wake",
            delivery_metadata={"kanban_source": True, "kanban_origin_session_id": key}))
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "completed", {"summary": "handoff", "artifacts": artifacts})
        return tid
    finally:
        conn.close()


def _sub(tid):
    conn = kbc.connect()
    try:
        return kbn.list_notify_subs(conn, tid)[0], kbn.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="42", kinds=["completed"])[1]
    finally:
        conn.close()


async def _tick(runner, failures):
    deliveries = await asyncio.to_thread(_notifier_collect, runner, kb,
        notifier_profile=None, gc_due=False, gc_retention_days=30)
    for delivery in deliveries:
        await _KanbanNotification(runner, delivery, platform_cls=Platform,
                                  sub_fail_counts=failures).deliver()


async def _drain(adapter):
    while adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks))


@pytest.mark.asyncio
async def test_undeliverable_origin_artifact_reaches_wake_without_resending_text(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    runner, adapter, key = _route()
    tid = _source_task(key, [str(tmp_path / "report.md")])  # declared, never written
    woke = []

    async def handler(event):
        woke.append(event.text)

    adapter.set_message_handler(handler)
    await adapter.connect()
    try:
        await _tick(runner, {})
        await _drain(adapter)
        await _tick(runner, {})
        await _drain(adapter)
    finally:
        await adapter.disconnect()
    assert len(adapter.wire) == 1
    assert len(woke) == 1 and "report.md: missing" in woke[0]
    sub, unseen = _sub(tid)
    assert unseen == []
    assert sub["delivery_metadata"]["kanban_source"] is True


@pytest.mark.asyncio
async def test_failed_origin_send_is_retried_not_parked(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    runner, adapter, key = _route()
    tid = _source_task(key, [])
    real_send = adapter.send
    calls = []

    async def flaky_send(chat_id, content, reply_to=None, metadata=None):
        calls.append(content)
        if len(calls) == 1:
            raise ConnectionError("transient transport failure")
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    monkeypatch.setattr(adapter, "send", flaky_send)
    adapter.set_message_handler(lambda event: asyncio.sleep(0))
    await adapter.connect()
    failures = {}
    try:
        await _tick(runner, failures)
        assert adapter.wire == [] and _sub(tid)[1]  # rewound for retry
        await _tick(runner, failures)
        await _drain(adapter)
    finally:
        await adapter.disconnect()
    assert len(adapter.wire) == 1
    assert _sub(tid)[1] == []
    assert failures == {}
