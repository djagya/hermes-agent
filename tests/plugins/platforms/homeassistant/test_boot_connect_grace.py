"""dlz fork patch on the bundled homeassistant plugin (re-lands fork #97, 9182a191b0).

Cold boot: when the gateway and Home Assistant start together, HA's port opens a minute or two after
the gateway's first connect. Within a 180 s grace window the first connect retries quietly on a short
backoff instead of logging ERROR/WARNING; past it the normal reconnect loop and its warnings apply.
"""

import errno
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from homeassistant_plugin.adapter import HomeAssistantAdapter


def _make_adapter(**extra) -> HomeAssistantAdapter:
    config = PlatformConfig(enabled=True, token="tok", extra=extra)
    adapter = HomeAssistantAdapter(config)
    adapter.handle_message = AsyncMock()
    return adapter

# ---------------------------------------------------------------------------
# Cold-boot connect: HA starts after the gateway on the same host
# ---------------------------------------------------------------------------


def _refused() -> Exception:
    import aiohttp

    return aiohttp.ClientConnectionError("Cannot connect to host 127.0.0.1:8123 [Connect call failed]")


def _loud_records(caplog):
    return [r for r in caplog.records if r.levelno >= 30 and "homeassistant" in r.name]


class TestBootConnectGrace:
    @pytest.mark.asyncio
    async def test_ha_not_up_yet_connects_in_background_without_warnings(self, monkeypatch, caplog):
        """Field case: every boot logged ERROR "Failed to connect" plus the watcher's "failed to
        connect", and only reconnected ~1 minute later. Now connect() returns at once with status
        ``retrying`` and finishes quietly on a short backoff."""
        caplog.set_level("DEBUG")
        adapter = _make_adapter(watch_all=True)
        monkeypatch.setattr(adapter, "_BOOT_BACKOFF_STEPS", [0])
        adapter._ws_connect = AsyncMock(side_effect=[_refused(), _refused(), True])
        adapter._listen_loop = AsyncMock()
        adapter._mark_connected = MagicMock()

        assert await adapter.connect() is True
        assert adapter.send_path_degraded is True  # startup publishes "retrying", not "connected"
        assert adapter._listen_task is not None
        await adapter._listen_task

        assert adapter._ws_connect.await_count == 3
        assert adapter.send_path_degraded is False
        adapter._mark_connected.assert_called_once()
        adapter._listen_loop.assert_awaited_once()
        assert _loud_records(caplog) == []
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_grace_exceeded_warns_once_then_normal_reconnect_loop(self, monkeypatch, caplog):
        adapter = _make_adapter(watch_all=True)
        monkeypatch.setattr(adapter, "_BOOT_BACKOFF_STEPS", [0])
        monkeypatch.setattr(adapter, "_BOOT_GRACE_SECONDS", 0.0)
        adapter._ws_connect = AsyncMock(side_effect=_refused())
        adapter._listen_loop = AsyncMock()

        assert await adapter.connect() is True
        assert adapter._listen_task is not None
        await adapter._listen_task

        loud = _loud_records(caplog)
        assert [r.levelname for r in loud] == ["WARNING"], loud
        assert "still unreachable" in loud[0].getMessage()
        adapter._listen_loop.assert_awaited_once()  # existing reconnect behaviour from here
        assert adapter.send_path_degraded is True  # until the reconnect loop actually connects
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_reconnect_listen_loop_clears_degraded_status(self, monkeypatch):
        """When the grace ran out first, the normal reconnect loop's success publishes "connected"."""
        adapter = _make_adapter(watch_all=True)
        adapter._running = True
        adapter._awaiting_first_connect = True
        monkeypatch.setattr(adapter, "_BACKOFF_STEPS", [0])
        adapter._mark_connected = MagicMock()

        async def _connected():
            adapter._running = False  # one reconnect pass, then the loop exits
            return True

        adapter._ws_connect = AsyncMock(side_effect=_connected)
        await adapter._listen_loop()
        assert adapter.send_path_degraded is False
        adapter._mark_connected.assert_called_once()

    @pytest.mark.asyncio
    async def test_watcher_reconnect_and_real_errors_fail_as_before(self, caplog):
        """Only a cold boot waits quietly: a watcher reconnect, or an error that isn't "not up yet",
        still fails the connect and logs at error."""
        adapter = _make_adapter(watch_all=True)
        adapter._ws_connect = AsyncMock(side_effect=_refused())
        assert await adapter.connect(is_reconnect=True) is False
        adapter._ws_connect = AsyncMock(side_effect=RuntimeError("unexpected payload"))
        assert await adapter.connect() is False
        assert [r.levelname for r in _loud_records(caplog)] == ["ERROR", "ERROR"]
        assert adapter._listen_task is None

    def test_not_up_yet_classification(self):
        import aiohttp

        def handshake(status):
            return aiohttp.WSServerHandshakeError(MagicMock(), (), status=status, message="x")

        not_up = (_refused(), ConnectionRefusedError(errno.ECONNREFUSED, "refused"), TimeoutError(), handshake(503))
        for exc in not_up:
            assert HomeAssistantAdapter._is_not_up_yet(exc), exc
        for exc in (handshake(401), handshake(404), RuntimeError("auth failed"), ValueError("bad json")):
            assert not HomeAssistantAdapter._is_not_up_yet(exc), exc
