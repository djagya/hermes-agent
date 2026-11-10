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


EVENT_ID = "e" * 32
RECORD = {"event": "handback", "session_id": "s-1", "slot_id": "shopping", "lease_id": "lease-1",
          "resumed_lease_id": "lease-2", "event_id": EVENT_ID}


def _control_confirms(monkeypatch, *records, asked=None):
    """Control's outbox: GET /v1/handback/pending answers only these exact events."""
    by_id = {r["event_id"]: r for r in records}

    def get_json(path, timeout):
        eid = path.rsplit("=", 1)[1]
        if asked is not None:
            asked.append(eid)
        return dict(by_id.get(eid, {}), pending=eid in by_id)

    monkeypatch.setattr(route, "get_json", get_json)


def _run(runner, session="s-1", lease="lease-1", event_id=EVENT_ID, **kw):
    kw.setdefault("resumed_lease_id", "lease-2")
    return asyncio.run(hb.inject(runner, session, "shopping", lease, event_id=event_id, **kw))


def test_a_local_claim_alone_never_wakes_control_must_confirm_every_event(monkeypatch):
    """Codex recheck P1: a minted handoff and a held lease, but control confirms nothing: no wake."""
    calls, asked = [], []
    runner = _runner(calls)
    route.note_handoff("s-1", "shopping", "lease-1", 1200)
    route._held_leases["s-1|shopping|research"] = {route.MANAGED_SLOT_ENV: "shopping",
                                                   route.MANAGED_LEASE_ENV: "lease-1"}
    _control_confirms(monkeypatch, asked=asked)
    assert _run(runner) == "refused"
    assert _run(runner, event="handoff_ended", reason_code="human_ended", event_id="b" * 32) == "refused"
    assert _run(runner, event_id="") == "refused"  # nothing control could confirm: not even asked
    assert calls == [] and asked == [EVENT_ID, "b" * 32]


def test_only_control_confirmation_of_the_exact_event_wakes_once(monkeypatch):
    calls = []
    runner = _runner(calls)
    _control_confirms(monkeypatch, RECORD)
    # Forged id, other session, other lease, other resumed lease: refused, nothing woken.
    assert _run(runner, event_id="f" * 32) == "refused"
    assert _run(runner, session="s-2") == "refused" and _run(runner, lease="lease-9") == "refused"
    assert _run(runner, resumed_lease_id="lease-7") == "refused" and calls == []
    monkeypatch.setattr(route, "get_json", lambda path, timeout: {"error": "unavailable"})
    assert _run(runner) == "unavailable"  # control did not answer: retry, never a wake
    _control_confirms(monkeypatch, RECORD)
    assert _run(runner) == "injected" and len(calls) == 1
    assert calls[0]["session_key"] == "telegram:dm:1" and "not approval" in calls[0]["content"]
    assert _run(runner) == "replay" and len(calls) == 1  # control did not see the 202: idempotent


def test_expired_link_ask_and_distinct_confirmed_ends_each_wake(monkeypatch):
    calls = []
    run = lambda reason, eid: _run(_runner(calls), event="handoff_ended", reason_code=reason, event_id=eid)
    end = {"event": "handoff_ended", "session_id": "s-1", "slot_id": "shopping", "lease_id": "lease-1"}
    _control_confirms(monkeypatch, {**end, "reason_code": "expired_unused", "event_id": "a" * 32},
                      {**end, "reason_code": "human_ended", "event_id": "c" * 32})
    assert run("expired_unused", "a" * 32) == "injected"
    assert "after it had expired" in calls[0]["content"] and "action=handoff" in calls[0]["content"]
    assert run("human_idle", "a" * 32) == "replay"  # an admitted id never wakes again
    assert run("human_idle", "d" * 32) == "refused"  # an invented reason/id is not confirmed
    assert run("human_ended", "c" * 32) == "injected" and len(calls) == 2


def _dispatching_runner(dispatch):
    runner = _runner([])
    runner._dispatch_plugin_message_injection = dispatch
    runner._plugin_injection_accepting = lambda: True
    return runner


def test_dispatch_carries_the_authorized_session_and_the_event_id_and_a_restart_retry_reuses_it(monkeypatch):
    """Recheck P2: the dispatcher refuses a key that rotated to another conversation (it gets the
    authorized session id), and the event id is the durable FIFO admission id, so admit -> lost 202
    -> gateway restart -> retry is the same admission."""
    seen = []

    async def dispatch(**kw):
        seen.append(kw)
        return True

    runner = _dispatching_runner(dispatch)
    _control_confirms(monkeypatch, RECORD)
    assert _run(runner) == "injected"
    hb._handled.clear()  # gateway restart: the in-process receipt is gone
    assert _run(runner) == "injected"
    assert [(k["expected_session_id"], k["admission_id"]) for k in seen] == [("s-1", "handback:" + EVENT_ID)] * 2


def test_a_cancelled_request_still_records_its_admission_so_the_retry_is_a_replay(monkeypatch):
    """Recheck P2: cancelling the request mid-dispatch must not drop the receipt or release the
    in-flight marker before the dispatch finished."""
    gate, calls = None, []

    async def dispatch(**kw):
        calls.append(kw)
        await gate.wait()
        return True

    runner = _dispatching_runner(dispatch)
    _control_confirms(monkeypatch, RECORD)

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        first = asyncio.ensure_future(hb.inject(runner, "s-1", "shopping", "lease-1", event_id=EVENT_ID,
                                                resumed_lease_id="lease-2"))
        while not calls:
            await asyncio.sleep(0)
        first.cancel()
        second = asyncio.ensure_future(hb.inject(runner, "s-1", "shopping", "lease-1", event_id=EVENT_ID,
                                                 resumed_lease_id="lease-2"))
        for _ in range(20):
            await asyncio.sleep(0)
        during = second.result() if second.done() else "dispatched again"  # a regression reaches dispatch
        gate.set()
        await asyncio.wait_for(second, timeout=5)
        await asyncio.wait_for(asyncio.gather(*hb._owners), timeout=5)
        after = await hb.inject(runner, "s-1", "shopping", "lease-1", event_id=EVENT_ID,
                                resumed_lease_id="lease-2")
        return during, after

    assert asyncio.run(scenario()) == ("unavailable", "replay")
    assert len(calls) == 1 and not hb._inflight


def test_concurrent_events_serialize_and_the_dispatcher_decides(monkeypatch):
    """Two events racing on one (session, slot, lease) are admitted one at a time; a dispatcher that
    refuses (draining gateway) is 'not admitted', even though scheduling would have said True."""
    calls, results = [], [False, True]
    gate = asyncio.Event()

    async def dispatch(**kw):
        calls.append(kw)
        await gate.wait()
        return results.pop(0)

    runner = _dispatching_runner(dispatch)
    end = {"event": "handoff_ended", "session_id": "s-1", "slot_id": "shopping", "lease_id": "lease-1",
           "reason_code": "human_ended", "event_id": "c" * 32}
    _control_confirms(monkeypatch, RECORD, end)

    async def race():
        first = asyncio.ensure_future(hb.inject(runner, "s-1", "shopping", "lease-1", event_id=EVENT_ID,
                                                resumed_lease_id="lease-2"))
        while not calls:
            await asyncio.sleep(0)
        second = await hb.inject(runner, "s-1", "shopping", "lease-1", event="handoff_ended",
                                 reason_code="human_ended", event_id="c" * 32)
        gate.set()
        return await asyncio.wait_for(first, timeout=5), second

    assert asyncio.run(race()) == ("unavailable", "unavailable")  # dispatcher refused; loser retries
    assert _run(runner) == "injected" and len(calls) == 2


def test_a_local_session_is_woken_through_its_owning_profile_or_refused_terminally(monkeypatch, tmp_path):
    """Recheck P2: a CLI/TUI/Desktop session is woken through the profile authority that holds it
    live; with no live owner the answer is 404 once, not 30 minutes of 503s."""
    import gateway.session_authorities as authorities
    from gateway.config import Platform

    seen = []

    async def dispatch(**kw):
        seen.append(kw)
        return True

    runner = _dispatching_runner(dispatch)

    class Store:
        async def lookup_by_session_id(self, sid):
            return SimpleNamespace(session_key="local:s-1", origin=SimpleNamespace(platform=Platform.LOCAL))

    runner.async_session_store = Store()
    live = SimpleNamespace(sessions={"s-1": SimpleNamespace(route="local:s-1")}, profile_id=str(tmp_path))
    monkeypatch.setattr(authorities, "all_authorities", lambda _runner: [live])
    _control_confirms(monkeypatch, RECORD)
    assert _run(runner) == "injected" and seen[0]["plugin_home"] == tmp_path
    live.sessions.clear()
    hb._handled.clear()
    assert _run(runner) == "session_not_found" and len(seen) == 1


@pytest.mark.asyncio
async def test_the_fifo_admits_one_event_id_once_and_refuses_a_rotated_session(tmp_path, monkeypatch):
    """Real session store: the same admission id twice (a newer human turn in between) is one FIFO
    row; an authorized session id the key no longer resolves to is refused."""
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress_context import native_callback
    from hermes_state_runtime import list_session_admissions
    from tests.gateway.test_automation_notification_category import _SOURCE, _native_owner

    runner, authority, adapter, _source, metadata = await _native_owner(tmp_path, monkeypatch, "human-1")
    runner._running = True
    try:
        route_key, sid = metadata["gateway_session_key"], metadata["gateway_session_id"]
        kw = dict(session_key=route_key, content="notice", plugin_id=hb.PLUGIN_ID, expected_session_id=sid,
                  admission_id="handback:" + EVENT_ID)
        assert await runner._dispatch_plugin_message_injection(**kw) is True
        human = MessageEvent(text="human 2", source=adapter.build_source(**_SOURCE, message_id="human-2"),
                             message_id="human-2")
        with native_callback(runner, human, tmp_path):
            await authority.admit_native(human)
        assert await runner._dispatch_plugin_message_injection(**kw) is True
        rows = [r for r in list_session_admissions(authority.db, session_id=authority.logical_owner(sid),
                                                   pending_only=False)
                if (r["payload"].get("native_text_v1") or {}).get("automation")]
        assert len(rows) == 1
        # /new rotated the key: an event authorized for an older conversation is refused there.
        assert await runner._dispatch_plugin_message_injection(
            **{**kw, "expected_session_id": "s-older", "admission_id": "handback:" + "f" * 32}) is False
        # A replay whose borrowed envelope changed conflicts on the digest: the row at that identity
        # exists, so it is the same admission; a conflict for an id never admitted stays refused.
        from hermes_state_runtime import RuntimeStoreError

        async def conflict(*_a, **_k):
            raise RuntimeStoreError("admission_conflict")

        monkeypatch.setattr(authority, "admit_automation", conflict)
        assert await runner._dispatch_plugin_message_injection(**kw) is True
        assert await runner._dispatch_plugin_message_injection(**{**kw, "admission_id": "handback:" + "d" * 32}) is False
    finally:
        authority.db.close()
