"""Return to Sera wakes the session that holds the slot; only control's key may ask, and it cannot choose the text."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server_slot_handback as hb


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "control-key")


def test_bearer_must_match_the_control_key(monkeypatch):
    assert hb.check_bearer("Bearer control-key") is None
    assert hb.check_bearer("Bearer api-key") == 401
    assert hb.check_bearer("") == 401
    monkeypatch.delenv("HERMES_BROWSER_CONTROL_KEY")
    assert hb.check_bearer("Bearer control-key") == 503


def test_parse_rejects_malformed_ids():
    assert hb.parse({"event": "handback", "session_id": "s-1", "slot_id": "shopping"})[0] is not None
    assert hb.parse({"event": "handback", "session_id": "s 1; rm", "slot_id": "shopping"})[0] is None
    assert hb.parse({"event": "other", "session_id": "s-1", "slot_id": "shopping"})[0] is None


def test_inject_targets_the_owning_session_with_gateway_text():
    calls = []

    class Store:
        async def lookup_by_session_id(self, sid):
            return SimpleNamespace(session_key="telegram:dm:1") if sid == "s-1" else None

    runner = SimpleNamespace(async_session_store=Store(),
                             _schedule_plugin_message_injection=lambda **kw: (calls.append(kw), True)[1])
    assert asyncio.run(hb.inject(runner, "s-1", "shopping")) == "injected"
    assert calls[0]["session_key"] == "telegram:dm:1" and "slot shopping is back" in calls[0]["content"]
    assert asyncio.run(hb.inject(runner, "s-unknown", "shopping")) == "session_not_found"
