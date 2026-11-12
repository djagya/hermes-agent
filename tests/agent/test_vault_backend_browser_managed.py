"""dlz: the real-profile ``browser:`` login source never runs in monolith managed browser mode.

Its reads launch or attach the user's local Chromium over direct CDP, outside browser-control leases
and handoff admission; managed mode allows no browser except a leased slot. Fail closed: a broken
managed probe counts as managed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.vault_backends import backend_for_handle, enabled_backends
from agent.vault_backends.browser import BrowserProfileLoginBackend

_HANDLE = "browser:https://example.com/login\x1fjane@example.com"


_reached: list[str] = []


def _boom(*_a, **_k):
    _reached.append("real-profile")  # list_items swallows exceptions, so record the reach as well
    raise AssertionError("real-profile browser reached in managed mode")


@pytest.fixture
def managed(monkeypatch):
    _reached.clear()
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://browser-control:8080")
    with patch("agent.vault_backends.base._browser_profile_enabled", return_value=True), \
         patch("hermes_cli.browser_connect.detect_default_chromium", return_value="chrome"), \
         patch("tools.browser_session_real_profile.real_profile_cdp", side_effect=_boom), \
         patch("tools.browser_session_real_profile.terminate_real_profile_browser", side_effect=_boom):
        yield


def test_managed_mode_drops_the_source_and_its_handles(managed):
    assert "browser" not in [b.name for b in enabled_backends()]
    assert backend_for_handle(_HANDLE) is None


def test_managed_mode_backend_refuses_direct_use(managed):
    backend = BrowserProfileLoginBackend()
    assert backend.list_items() == []
    assert backend.get_meta(_HANDLE) is None
    with pytest.raises(RuntimeError, match="managed"):
        backend.resolve_password(_HANDLE)
    assert _reached == []


def test_broken_managed_probe_fails_closed(monkeypatch):
    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    with patch("agent.vault_backends.base._browser_profile_enabled", return_value=True), \
         patch("tools.browser_control_route.is_managed", side_effect=RuntimeError("probe broke")), \
         patch("hermes_cli.browser_connect.detect_default_chromium", return_value="chrome"), \
         patch("tools.browser_session_real_profile.real_profile_cdp", side_effect=_boom):
        assert "browser" not in [b.name for b in enabled_backends()]
        with pytest.raises(RuntimeError, match="managed"):
            BrowserProfileLoginBackend().resolve_password(_HANDLE)
