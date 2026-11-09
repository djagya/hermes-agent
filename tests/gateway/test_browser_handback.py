"""Return to Sera wakes only the session that minted that handoff, once; the key alone is not enough."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server_slot_handback as hb
from tools import browser_control_route as route


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "control-key")
    hb._handled.clear()
    hb._inflight.clear()
    route._minted_handoffs.clear()
    route.clear_held_leases()


def test_bearer_must_match_the_control_key(monkeypatch):
    assert hb.check_bearer("Bearer control-key") is None
    assert hb.check_bearer("Bearer api-key") == 401
    assert hb.check_bearer("") == 401
    monkeypatch.delenv("HERMES_BROWSER_CONTROL_KEY")
    assert hb.check_bearer("Bearer control-key") == 503


def test_parse_rejects_malformed_ids():
    ok = {"event": "handback", "session_id": "s-1", "slot_id": "shopping", "lease_id": "lease-1"}
    assert hb.parse(ok)[0] is not None
    assert hb.parse({**ok, "session_id": "s 1; rm"})[0] is None
    assert hb.parse({**ok, "lease_id": ""})[0] is None
    assert hb.parse({**ok, "event": "other"})[0] is None


def _runner(calls):
    class Store:
        async def lookup_by_session_id(self, sid):
            return SimpleNamespace(session_key="telegram:dm:1") if sid == "s-1" else None

    return SimpleNamespace(async_session_store=Store(),
                           _schedule_plugin_message_injection=lambda **kw: (calls.append(kw), True)[1])


def test_only_a_minted_handoff_wakes_and_only_once():
    calls = []
    runner = _runner(calls)
    assert asyncio.run(hb.inject(runner, "s-1", "shopping", "lease-1")) == "refused"  # key alone
    route.note_handoff("s-1", "shopping", "lease-1", 1200)
    assert asyncio.run(hb.inject(runner, "s-1", "research-1", "lease-1")) == "refused"  # wrong slot
    assert asyncio.run(hb.inject(runner, "s-1", "shopping", "lease-1")) == "injected"
    assert calls[0]["session_key"] == "telegram:dm:1" and "not approval" in calls[0]["content"]
    route._held_leases["s-1|shopping|research"] = {route.MANAGED_SLOT_ENV: "shopping", route.MANAGED_LEASE_ENV: "lease-1"}
    assert asyncio.run(hb.inject(runner, "s-1", "shopping", "lease-1")) == "replay"
    assert len(calls) == 1


# dlz: durable delivery (Codex qol #10). Control retries with one event_id; the gateway admits once.
EVENT_ID = "e" * 32


def _flaky_runner(calls, results):
    runner = _runner(calls)
    runner._schedule_plugin_message_injection = lambda **kw: (calls.append(kw), results.pop(0))[1]
    return runner


def test_scheduling_failure_keeps_the_claim_so_the_retry_wakes_once(monkeypatch):
    calls = []
    runner = _flaky_runner(calls, [False, True])
    route.note_handoff("s-1", "shopping", "lease-1", 1200)
    run = lambda: asyncio.run(hb.inject(runner, "s-1", "shopping", "lease-1", event_id=EVENT_ID))
    assert run() == "unavailable"  # not admitted: no marker recorded, claim not consumed
    assert run() == "injected"
    # Control did not see the 202 and sends the same event again: its outbox still holds it.
    monkeypatch.setattr(route, "get_json", lambda path, timeout: {
        "pending": True, "event": "handback", "session_id": "s-1", "slot_id": "shopping",
        "lease_id": "lease-1", "resumed_lease_id": "", "event_id": EVENT_ID})
    assert run() == "replay" and len(calls) == 2  # the duplicate is idempotent


def test_after_a_gateway_restart_only_control_confirmation_of_the_exact_event_wakes(monkeypatch):
    calls, asked = [], []
    record = {"pending": True, "event": "handback", "session_id": "s-1", "slot_id": "shopping",
              "lease_id": "lease-1", "resumed_lease_id": "lease-2", "event_id": EVENT_ID}
    replies = {EVENT_ID: record}
    monkeypatch.setattr(route, "get_json", lambda path, timeout: (asked.append(path), dict(
        replies.get(path.rsplit("=", 1)[1], {"pending": False})))[1])
    runner = _runner(calls)

    def run(session="s-1", lease="lease-1", event_id=EVENT_ID):
        return asyncio.run(hb.inject(runner, session, "shopping", lease, event_id=event_id,
                                     resumed_lease_id="lease-2"))

    # No local claim (restart). Forged id, other session, other lease, no id: refused, nothing woken.
    assert run(event_id="f" * 32) == "refused"
    assert run(session="s-2") == "refused" and run(lease="lease-9") == "refused"
    assert run(event_id="") == "refused" and calls == []
    replies.clear()
    monkeypatch.setattr(route, "get_json", lambda path, timeout: {"error": "unavailable"})
    assert run() == "unavailable"  # control did not answer: retry, never a wake
    monkeypatch.setattr(route, "get_json", lambda path, timeout: dict(record))
    assert run() == "injected" and len(calls) == 1
    assert run() == "replay" and len(calls) == 1


def _control_confirms(monkeypatch, *records):
    """Control's outbox: GET /v1/handback/pending answers only these exact events."""
    by_id = {r["event_id"]: r for r in records}
    monkeypatch.setattr(route, "get_json", lambda path, timeout: dict(
        by_id.get(path.rsplit("=", 1)[1], {"pending": False}), pending=path.rsplit("=", 1)[1] in by_id))


def test_expired_link_ask_reaches_the_minting_session_and_a_later_end_needs_control(monkeypatch):
    calls = []
    route.note_handoff("s-1", "shopping", "lease-1", 1200)
    run = lambda reason, eid: asyncio.run(hb.inject(_runner(calls), "s-1", "shopping", "lease-1",
                                                    event="handoff_ended", reason_code=reason, event_id=eid))
    assert run("expired_unused", "a" * 32) == "injected"
    assert "after it had expired" in calls[0]["content"] and "action=handoff" in calls[0]["content"]
    # Review P1: more end notices for the lease on the key alone (invented reasons) are replays.
    route._held_leases["s-1|shopping|research"] = {route.MANAGED_SLOT_ENV: "shopping",
                                                   route.MANAGED_LEASE_ENV: "lease-1"}
    _control_confirms(monkeypatch)
    assert run("human_ended", "b" * 32) == "replay" and run("human_idle", "") == "replay"
    # A genuine later End that control confirms is its own notice.
    _control_confirms(monkeypatch, {"event": "handoff_ended", "session_id": "s-1", "slot_id": "shopping",
                                    "lease_id": "lease-1", "reason_code": "human_ended", "event_id": "c" * 32})
    assert run("human_ended", "c" * 32) == "injected" and len(calls) == 2
    assert run("human_ended", "c" * 32) == "replay"  # its retry is idempotent


def test_concurrent_events_cannot_spend_one_claim_twice_and_the_dispatcher_decides(monkeypatch):
    """Review P2: handback + handoff_ended racing on one minted claim wake once; a dispatcher that
    refuses (draining gateway) is 'not admitted', even though scheduling would have said True."""
    calls, results = [], [False, True]
    gate = asyncio.Event()

    async def dispatch(**kw):
        calls.append(kw)
        await gate.wait()
        return results.pop(0)

    runner = _runner([])
    runner._dispatch_plugin_message_injection = dispatch
    runner._plugin_injection_accepting = lambda: True
    route.note_handoff("s-1", "shopping", "lease-1", 1200)

    async def race():
        first = asyncio.ensure_future(hb.inject(runner, "s-1", "shopping", "lease-1"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(hb.inject(runner, "s-1", "shopping", "lease-1", event="handoff_ended",
                                                 reason_code="human_ended"))
        await asyncio.sleep(0)
        loser = second.result() if second.done() else "dispatched"  # a regression reaches dispatch
        gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
        return first.result(), loser

    assert asyncio.run(race()) == ("unavailable", "unavailable")  # dispatcher refused; loser retries
    assert asyncio.run(hb.inject(runner, "s-1", "shopping", "lease-1")) == "injected"
    assert len(calls) == 2
