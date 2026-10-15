"""Cron delivery (``_standalone_send``) calls the notify service its target names (#62)."""

from __future__ import annotations

import pytest

from gateway.config import PlatformConfig


@pytest.fixture
def ha_post(monkeypatch):
    """Record every POST and answer from ``statuses`` (url suffix -> HTTP status, default 200)."""
    import aiohttp

    calls, statuses = [], {}

    class _Resp:
        def __init__(self, status):
            self.status = status
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def text(self): return "Server got itself in trouble" if self.status == 500 else ""

    class _Sess:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def post(self, url, *, json=None, **kw):
            path = url.split("/api/services/", 1)[1]
            calls.append((path, json))
            return _Resp(statuses.get(path, 200))

    monkeypatch.setattr(aiohttp, "ClientSession", _Sess)
    monkeypatch.setenv("HASS_URL", "http://ha.local:8123")
    monkeypatch.setenv("HASS_TOKEN", "tok")
    return calls, statuses


async def _send(chat_id):
    from homeassistant_plugin import adapter as ha
    return await ha._standalone_send(PlatformConfig(enabled=True), chat_id, "hi")


@pytest.mark.asyncio
async def test_target_is_called_as_its_own_notify_service(ha_post):
    calls, statuses = ha_post
    statuses["notify/notify"] = 500  # current HA: grouped notify.notify + target
    result = await _send("mobile_app_my_phone")
    assert result["success"] is True
    assert calls == [("notify/mobile_app_my_phone", {"message": "hi"})]


@pytest.mark.asyncio
async def test_notify_prefix_is_accepted(ha_post):
    calls, _ = ha_post
    assert (await _send("notify.mobile_app_my_phone"))["success"] is True
    assert calls == [("notify/mobile_app_my_phone", {"message": "hi"})]


@pytest.mark.asyncio
async def test_unknown_service_falls_back_to_grouped_notify_with_target(ha_post):
    calls, statuses = ha_post
    statuses["notify/living_room_tv"] = 400  # HA: service not found
    assert (await _send("living_room_tv"))["success"] is True
    assert calls == [("notify/living_room_tv", {"message": "hi"}),
                     ("notify/notify", {"message": "hi", "target": "living_room_tv"})]


@pytest.mark.asyncio
async def test_service_failure_other_than_not_found_is_reported_without_fallback(ha_post):
    calls, statuses = ha_post
    statuses["notify/mobile_app_my_phone"] = 500
    result = await _send("mobile_app_my_phone")
    assert result.get("success") is not True
    assert "500" in result["error"] and "notify/mobile_app_my_phone" in result["error"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_target_that_is_not_a_service_name_uses_grouped_notify(ha_post):
    calls, _ = ha_post
    assert (await _send("Matt's iPhone"))["success"] is True
    assert calls == [("notify/notify", {"message": "hi", "target": "Matt's iPhone"})]
