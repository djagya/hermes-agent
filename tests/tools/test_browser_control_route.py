"""Managed-mode routing: ambient CDP/cloud cannot bypass browser-control."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from tools.browser_control_route import (
    MANAGED_LEASE_ENV,
    MANAGED_TOKEN_ENV,
    ManagedBrowserError,
    clear_held_leases,
    control_url,
    daemon_cache_key,
    is_managed,
    managed_cdp_or_error,
    release_lease,
    resolve_managed_cdp,
)
from tools.browser_use_cli import _resolve_backend_cdp, browser_exec


DEFAULT_LEASE = {
    "lease_id": "lease-1",
    "slot_id": "research-1",
    "browser_generation": "bgen-1",
    "ownership_epoch": 2,
    "command_token": "tok-1",
    "owner": "agent",
}


class _State:
    def __init__(self):
        self.acquires = []
        self.releases = []
        self.auth = []
        self.acquire_replies = []


def _start(state):
    class _Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            state.auth.append(self.headers.get("Authorization"))
            if self.path.rstrip("/").endswith("/v1/release"):
                state.releases.append(payload)
                body = {"ok": True}
            else:
                state.acquires.append(payload)
                if state.acquire_replies:
                    body = state.acquire_replies.pop(0)
                else:
                    body = dict(DEFAULT_LEASE)
            raw = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = HTTPServer(("127.0.0.1", 0), _Control)
    Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


@pytest.fixture(autouse=True)
def _reset_held():
    clear_held_leases()
    yield
    clear_held_leases()


def _managed_env(monkeypatch, port, key="secret-key"):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", key)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("BU_AUTOSPAWN", raising=False)


def test_daemon_cache_key_changes_with_epoch():
    assert daemon_cache_key("research-1", "g", 1) != daemon_cache_key("research-1", "g", 2)


def test_bearer_sent_and_acquire_sets_gated_url(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    assert is_managed()
    env = {}
    err = resolve_managed_cdp(
        env,
        task_id="t1",
        session_name="s1",
        mode="qa",
        identity="work:acme",
        qa_targets=["app.internal"],
    )
    httpd.shutdown()
    assert err is None
    assert state.auth == ["Bearer secret-key"]
    assert state.acquires[0]["mode"] == "qa"
    assert state.acquires[0]["identity"] == "work:acme"
    assert state.acquires[0]["qa_targets"] == ["app.internal"]
    assert env["BU_CDP_URL"].endswith("/slot/research-1/json/version?tok=tok-1")
    assert env["BU_NAME"] == daemon_cache_key("research-1", "bgen-1", 2)
    assert env["_HERMES_BU_PRIVATE_BROWSER"] == "1"


def test_config_control_url_used_when_env_absent(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"browser": {"control_url": f"http://127.0.0.1:{port}"}},
    )
    assert control_url().endswith(f":{port}")
    env = {}
    err = resolve_managed_cdp(env, task_id="t1")
    httpd.shutdown()
    assert err is None
    assert env["BU_NAME"].startswith("bu-ctrl-")


def test_disabled_sentinel_fails_closed_not_unmanaged(monkeypatch):
    from tools.browser_control_route import OPERATOR_DISABLED_ERROR, is_managed

    state = _State()
    httpd, _port = _start(state)
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "disabled://")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    assert is_managed() is True
    env = {"BU_CDP_URL": "http://127.0.0.1:9222"}
    err = resolve_managed_cdp(env, task_id="t1")
    cdp, cdp_err = managed_cdp_or_error(task_id="t1")
    httpd.shutdown()
    assert err == OPERATOR_DISABLED_ERROR
    assert cdp is None and cdp_err == OPERATOR_DISABLED_ERROR
    assert state.acquires == []
    assert env.get("BU_CDP_URL") == "http://127.0.0.1:9222"


def test_disabled_config_url_fails_closed(monkeypatch):
    from tools.browser_control_route import OPERATOR_DISABLED_ERROR

    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"browser": {"control_url": "disabled://"}},
    )
    assert control_url() == "disabled://"
    assert is_managed() is True
    err = resolve_managed_cdp({}, task_id="t1")
    assert err == OPERATOR_DISABLED_ERROR


def test_unreadable_config_fails_closed_not_unmanaged(monkeypatch):
    from tools.browser_control_route import CONFIG_UNREADABLE_URL, is_managed

    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")

    def boom():
        raise OSError("config.yaml: permission denied")

    monkeypatch.setattr("hermes_cli.config.read_raw_config", boom)
    assert control_url() == CONFIG_UNREADABLE_URL
    assert is_managed() is True
    env = {}
    err = resolve_managed_cdp(env, task_id="t1")
    assert err is not None and err.startswith("unavailable")
    assert "BU_CDP_URL" not in env


def test_env_control_url_wins_over_config(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"browser": {"control_url": "http://127.0.0.1:1"}},
    )
    assert control_url() == f"http://127.0.0.1:{port}"
    err = resolve_managed_cdp({}, task_id="t1")
    httpd.shutdown()
    assert err is None


def test_busy_returns_retry_error(monkeypatch):
    state = _State()
    state.acquire_replies.append({"error": "busy", "detail": "queued"})
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    err = resolve_managed_cdp({}, task_id="t1")
    httpd.shutdown()
    assert err and err.startswith("busy:")
    assert "retry" in err
    assert "cancel" in err


def test_stale_reacquires_once(monkeypatch):
    state = _State()
    state.acquire_replies.append({"error": "stale", "detail": "epoch moved"})
    state.acquire_replies.append(dict(DEFAULT_LEASE))
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    env = {}
    err = resolve_managed_cdp(env, task_id="t1", reconnect=True)
    httpd.shutdown()
    assert err is None
    assert len(state.acquires) == 2
    assert env["BU_NAME"] == daemon_cache_key("research-1", "bgen-1", 2)


def test_release_lease_posts_id_and_token(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    env = {
        MANAGED_LEASE_ENV: "lease-1",
        MANAGED_TOKEN_ENV: "tok-1",
        "BU_NAME": daemon_cache_key("research-1", "bgen-1", 2),
    }
    err = release_lease(env)
    httpd.shutdown()
    assert err is None
    assert state.releases == [{"lease_id": "lease-1", "token": "tok-1"}]
    assert state.auth == ["Bearer secret-key"]


def test_cleanup_releases_managed_session(monkeypatch):
    import tools.browser_tool as browser_tool
    import tools.browser_tool_cdp as bt_cdp
    import tools.browser_tool_lifecycle as lifecycle
    import tools.browser_tool_session as session

    state = _State()
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    monkeypatch.setattr(bt_cdp, "_stop_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(browser_tool, "_maybe_stop_recording", lambda task_id: None)
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: {"success": True})
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_active_sessions", {
        "task-1": {
            "session_name": "cdp_managed",
            "bb_session_id": None,
            "cdp_url": "http://127.0.0.1/slot/research-1/json/version?tok=tok-1",
            "features": {"managed": True, "cdp_override": True},
            MANAGED_LEASE_ENV: "lease-1",
            MANAGED_TOKEN_ENV: "tok-1",
        }
    })
    monkeypatch.setattr(browser_tool, "_session_last_activity", {"task-1": 0.0})
    lifecycle._cleanup_single_browser_session("task-1")
    httpd.shutdown()
    assert state.releases == [{"lease_id": "lease-1", "token": "tok-1"}]


def test_resolve_backend_cdp_managed_refuses_override(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    err = _resolve_backend_cdp({"BU_CDP_URL": "http://127.0.0.1:9222"}, "t1")
    assert err and "refuses" in err and "scope_denied" in err


def test_unmanaged_keeps_existing_bu_env(monkeypatch):
    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
    env = {"BU_CDP_WS": "ws://operator-override:9222"}
    assert _resolve_backend_cdp(env, "t1") is None
    assert env["BU_CDP_WS"] == "ws://operator-override:9222"


def test_builtin_get_cdp_override_raw_hides_ambient_in_managed(monkeypatch):
    import tools.browser_tool_cdp as bt_cdp

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    assert bt_cdp._get_cdp_override_raw() == ""


def test_builtin_session_refuses_cdp_url_override_in_managed(monkeypatch):
    import tools.browser_tool as browser_tool
    import tools.browser_tool_lifecycle as lifecycle
    import tools.browser_tool_session as session

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(lifecycle, "_update_session_activity", lambda task_id: None)
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_session_last_activity", {})
    with pytest.raises(ManagedBrowserError) as caught:
        session._get_session_info("task-nav")
    assert "scope_denied" in str(caught.value)
    assert "cdp_url" in str(caught.value) or "BROWSER_CDP" in str(caught.value)


def test_builtin_navigate_refuses_cdp_url_override_in_managed(monkeypatch):
    import tools.browser_tool as browser_tool
    import tools.browser_tool_lifecycle as lifecycle

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(lifecycle, "_update_session_activity", lambda task_id: None)
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    out = json.loads(browser_tool.browser_navigate("https://example.com", task_id="task-nav"))
    assert out["success"] is False
    assert "scope_denied" in out["error"]


def test_auto_local_disabled_in_managed(monkeypatch):
    import tools.browser_tool_cloud as bt_cloud

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    assert bt_cloud._auto_local_for_private_urls() is False


def test_managed_cdp_or_error_unmanaged_is_passthrough(monkeypatch):
    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
    assert managed_cdp_or_error() == (None, None)


def test_managed_skips_own_tab_preamble(tmp_path, monkeypatch):
    import stat

    state = _State()
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    stdin_path = tmp_path / "stdin.txt"
    script = tmp_path / "browser-use"
    script.write_text(f"#!/bin/sh\ncat > '{stdin_path}'\necho ok\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    import tools.browser_use_cli as bu_cli

    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(script)])
    result = json.loads(bu_cli.browser_exec("print(1)", session="s1"))
    httpd.shutdown()
    assert result["success"] is True
    posted = stdin_path.read_text()
    assert "_hermes_ensure_own_tab" not in posted
    assert "print(1)" in posted


def test_session_name_still_validated_in_managed(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    import tools.browser_use_cli as bu_cli

    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/bin/true"])
    result = json.loads(browser_exec("print(1)", session="has space"))
    assert "error" in result
    assert "Invalid session name" in result["error"]


def test_mode_defaults_to_research(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    resolve_managed_cdp({}, task_id="t1")
    httpd.shutdown()
    assert state.acquires[0]["mode"] == "research"
    assert state.acquires[0]["identity"] == ""
    assert state.acquires[0]["qa_targets"] == []


def test_browser_cdp_surfaces_managed_error(monkeypatch):
    from tools import browser_cdp_tool

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets"))
    assert "error" in result
    assert "scope_denied" in result["error"]
    assert "No CDP endpoint" not in result["error"]


def test_camofox_snapshot_skipped_in_managed(monkeypatch):
    import tools.browser_camofox as camofox
    import tools.browser_tool as browser_tool

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setenv("CAMOFOX_URL", "http://127.0.0.1:9377")
    monkeypatch.setattr(
        "tools.tool_backend_helpers.read_selection", lambda *_a, **_k: "camofox"
    )
    assert camofox.is_camofox_mode() is False
    called = []
    monkeypatch.setattr(
        camofox,
        "camofox_snapshot",
        lambda *a, **k: called.append(True) or json.dumps({"success": True, "via": "camofox"}),
    )
    import tools.browser_tool_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(lifecycle, "_update_session_activity", lambda task_id: None)
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_session_last_activity", {})
    monkeypatch.setattr(browser_tool, "_last_session_key", lambda task_id: task_id or "default")
    out = json.loads(browser_tool.browser_snapshot(task_id="task-snap"))
    assert called == []
    assert out.get("success") is False
    err = out.get("error", "")
    assert "scope_denied" in err or err.startswith("unavailable") or "unavailable" in err
