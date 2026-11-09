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
    assert asyncio.run(hb.inject(runner, "s-1", "shopping", "lease-1")) == "session_not_found"  # key alone
    route.note_handoff("s-1", "shopping", "lease-1", 1200)
    assert asyncio.run(hb.inject(runner, "s-1", "research-1", "lease-1")) == "session_not_found"  # wrong slot
    assert asyncio.run(hb.inject(runner, "s-1", "shopping", "lease-1")) == "injected"
    assert calls[0]["session_key"] == "telegram:dm:1" and "not approval" in calls[0]["content"]
    route._held_leases["s-1|shopping|research"] = {route.MANAGED_SLOT_ENV: "shopping", route.MANAGED_LEASE_ENV: "lease-1"}
    assert asyncio.run(hb.inject(runner, "s-1", "shopping", "lease-1")) == "replay"
    assert len(calls) == 1
