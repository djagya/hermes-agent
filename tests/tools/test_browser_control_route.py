"""Managed-mode routing: ambient CDP/cloud cannot bypass browser-control."""

import json
import os
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
    release_task_leases,
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
        self.status_calls = []
        # lease_id -> /v1/status reply; default: the lease is a live agent lease.
        self.status_replies = {}


def _start(state):
    class _Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            from urllib.parse import parse_qs, urlparse

            lease = (parse_qs(urlparse(self.path).query).get("lease") or [""])[0]
            state.status_calls.append(lease)
            body = state.status_replies.get(lease, {"owner": "agent", "state": "agent"})
            status = 409 if body.get("error") == "stale" else 200
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

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


def test_session_cleanup_releases_held_managed_leases(monkeypatch):
    """The browser session layer's exit cleanup hands every held lease back to control (v0.23
    browser_session_lifecycle; the old per-session hook went with the deleted built-in session module)."""
    import tools.browser_session_lifecycle as lifecycle

    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        assert _resolve(port, monkeypatch, task_id="task-1")[0] is None
        lifecycle._release_managed_leases()
        lifecycle._release_managed_leases()  # idempotent
    finally:
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
    monkeypatch.setattr("hermes_cli.config.read_raw_config", dict)
    env = {"BU_CDP_WS": "ws://operator-override:9222"}
    assert _resolve_backend_cdp(env, "t1") is None
    assert env["BU_CDP_WS"] == "ws://operator-override:9222"


def test_session_cdp_hides_every_ambient_override_in_managed(monkeypatch):
    import tools.browser_session_cdp as session_cdp

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(session_cdp, "_live_overrides", {"default": "ws://127.0.0.1:9222/devtools/browser/x"})
    monkeypatch.setattr("hermes_constants.hermes_home_key", lambda: "default")
    assert session_cdp.get_cdp_override_raw() == ""
    assert session_cdp.get_cdp_override() == ""
    assert session_cdp.live_cdp_override() == ""


def test_live_browser_connect_refused_in_managed(monkeypatch):
    import tools.browser_session_cdp as session_cdp

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(session_cdp, "_live_overrides", {})
    monkeypatch.setattr(session_cdp, "_live_generations", {})
    with pytest.raises(ManagedBrowserError, match="scope_denied"):
        session_cdp.set_live_cdp_override("http://127.0.0.1:9222")
    assert session_cdp._live_overrides == {}
    session_cdp.set_live_cdp_override(None)  # a disconnect stays allowed


def test_managed_probe_fails_closed_on_probe_error(monkeypatch):
    import tools.browser_session_cdp as session_cdp

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("tools.browser_control_route.is_managed", boom)
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    assert session_cdp.managed_browser() is True
    assert session_cdp.get_cdp_override_raw() == ""
    import tools.browser_use_cli as bu_cli
    import tools.browser_vault_tool as vault
    assert bu_cli._control_is_managed() is True
    assert vault._is_managed() is True


def test_create_session_fails_closed_when_route_import_breaks(monkeypatch):
    import sys

    import tools.browser_session as session

    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", dict)
    launched = []
    monkeypatch.setattr(session, "_create_local_session", lambda *a, **k: launched.append(a))
    monkeypatch.setitem(sys.modules, "tools.browser_control_route", None)
    with pytest.raises(RuntimeError, match="refusing local/cloud fallback"):
        session._create_session_for_key("task-nav", force_local=False)
    assert launched == []


def test_session_layer_refuses_every_browser_in_managed(monkeypatch):
    """No CDP override, cloud provider, sandbox, Lightpanda or packaged Chromium in managed mode, even
    with an ambient override set: a session request is a routing bug and fails closed."""
    import tools.browser_session as session
    import tools.browser_session_lifecycle as lifecycle

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    launched = []
    monkeypatch.setattr(session, "_create_local_session", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(lifecycle, "start_cleanup_thread", lambda: None)
    monkeypatch.setattr(lifecycle, "update_session_activity", lambda key: None)
    monkeypatch.setattr(session, "_active_sessions", {})
    for key in ("task-nav", "task-nav::local"):
        with pytest.raises(ManagedBrowserError, match="scope_denied"):
            session.get_session_info(key)
    assert launched == [] and session._active_sessions == {}


def test_browser_exec_route_refuses_ambient_cdp_url_in_managed(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    err = _resolve_backend_cdp({}, "task-nav")
    assert err and "scope_denied" in err and "BROWSER_CDP_URL" in err


def test_cloud_and_local_routing_disabled_in_managed(monkeypatch):
    import tools.browser_session_cloud as session_cloud

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    assert session_cloud.auto_local_for_private_urls() is False
    assert session_cloud.get_cloud_provider() is None
    assert session_cloud.is_local_backend() is False  # the private-network guard stays on


def test_managed_cdp_or_error_unmanaged_is_passthrough(monkeypatch):
    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", dict)
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


def test_managed_harness_env_is_hardened(tmp_path, monkeypatch):
    """B5: the managed harness child gets no control key, BH_RECORD=0, and the undumpable sitecustomize
    FIRST on PYTHONPATH, after browser_exec replaced the inherited PYTHONPATH with the harness site dir."""
    import stat

    import tools.browser_use_cli as bu_cli

    state = _State()
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    monkeypatch.setenv("PYTHONPATH", "/inherited")
    monkeypatch.setattr("tools.browser_process_hardening._linux", lambda: True)
    env_path = tmp_path / "env.txt"
    script = tmp_path / "browser-use"
    script.write_text(f"#!/bin/sh\nenv > '{env_path}'\ncat > /dev/null\necho ok\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(script)])
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t-env"))
    finally:
        httpd.shutdown()
    assert result["success"] is True, result
    child = dict(line.split("=", 1) for line in env_path.read_text().splitlines() if "=" in line)
    assert "HERMES_BROWSER_CONTROL_KEY" not in child and "HERMES_BROWSER_CONTROL_KEY_FILE" not in child
    assert child["BH_RECORD"] == "0"
    first, *rest = child["PYTHONPATH"].split(os.pathsep)
    assert first.endswith("hardening-site")
    assert "/inherited" not in rest
    assert child["BU_CDP_URL"].endswith("/json/version?tok=tok-1")


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


def test_plugin_page_backend_never_used_in_managed(monkeypatch):
    """Camofox and other plugin browsers register page backends (agent/browser_page_registry); a managed
    vault op never reaches one, whatever the registry holds."""
    import tools.browser_vault_tool as vault

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    called = []
    monkeypatch.setattr("agent.browser_page_registry.backend_for_task", lambda task_id: called.append(task_id) or object())
    assert vault._page_backend("task-snap") is None
    assert called == []
    out = json.loads(vault._fenced_page_op("task-snap", lambda: json.dumps({"success": True})))
    assert out["success"] is False and out["error_type"] == "no_browser_session"


def _resolve(port, monkeypatch, **kwargs):
    env = {}
    err = resolve_managed_cdp(env, raw_cdp_override="", **kwargs)
    return err, env


def test_cached_lease_is_reused_while_alive(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        assert _resolve(port, monkeypatch, task_id="t1")[0] is None
        err, env = _resolve(port, monkeypatch, task_id="t1")
        assert err is None and env[MANAGED_LEASE_ENV] == "lease-1"
        assert len(state.acquires) == 1 and state.status_calls == ["lease-1"]
    finally:
        httpd.shutdown()


def test_dead_cached_lease_reacquires(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        assert _resolve(port, monkeypatch, task_id="t1")[0] is None
        state.status_replies["lease-1"] = {"error": "stale", "detail": "unknown lease"}
        state.acquire_replies.append(dict(DEFAULT_LEASE, lease_id="lease-2", command_token="tok-2"))
        err, env = _resolve(port, monkeypatch, task_id="t1")
        assert err is None
        assert env[MANAGED_LEASE_ENV] == "lease-2" and env[MANAGED_TOKEN_ENV] == "tok-2"
        assert len(state.acquires) == 2
    finally:
        httpd.shutdown()


def test_cached_lease_never_crosses_identity_or_mode(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        assert _resolve(port, monkeypatch, task_id="t1", session_name="s")[0] is None
        state.acquire_replies.append(
            dict(DEFAULT_LEASE, lease_id="lease-s", slot_id="sensitive", command_token="tok-s")
        )
        err, env = _resolve(port, monkeypatch, task_id="t1", session_name="s", identity="sensitive")
        assert err is None and env[MANAGED_LEASE_ENV] == "lease-s"
        assert [a["identity"] for a in state.acquires] == ["", "sensitive"]
    finally:
        httpd.shutdown()


def test_session_name_is_not_shared_across_tasks(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        assert _resolve(port, monkeypatch, task_id="cron-run-1", session_name="x")[0] is None
        assert _resolve(port, monkeypatch, task_id="cron-run-2", session_name="x")[0] is None
        assert [a["task"] for a in state.acquires] == ["cron-run-1", "cron-run-2"]
    finally:
        httpd.shutdown()


def test_unreachable_control_while_checking_cache_fails_closed(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    assert _resolve(port, monkeypatch, task_id="t1")[0] is None
    httpd.shutdown()
    httpd.server_close()
    err, env = _resolve(port, monkeypatch, task_id="t1")
    assert err.startswith("unavailable:") and "BU_CDP_URL" not in env


def test_post_read_timeout_is_unavailable_not_a_crash(monkeypatch):
    import tools.browser_control_route as route

    def boom(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(route, "urlopen", boom)
    out = route._post("http://127.0.0.1:9", "/v1/acquire", {})
    assert out["error"] == "unavailable" and "timed out" in out["detail"]


def test_release_task_leases_releases_only_that_tasks_holds(monkeypatch):
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        assert _resolve(port, monkeypatch, task_id="cron:job:run-1")[0] is None
        state.acquire_replies.append(
            dict(DEFAULT_LEASE, lease_id="lease-2", slot_id="research-2", command_token="tok-2")
        )
        assert _resolve(port, monkeypatch, task_id="other-session")[0] is None
        assert release_task_leases({"cron:job:run-1", "", None}) == 1
        assert [r["lease_id"] for r in state.releases] == ["lease-1"]
        assert release_task_leases(set()) == 0
        # The other session's hold is untouched and still reused.
        err, env = _resolve(port, monkeypatch, task_id="other-session")
        assert err is None and env[MANAGED_LEASE_ENV] == "lease-2"
    finally:
        httpd.shutdown()


def test_agent_close_releases_browser_exec_leases(monkeypatch):
    import tools.browser_control_route as route
    from agent.client_lifecycle import ClientLifecycleMixin

    seen = []
    monkeypatch.setattr(route, "release_task_leases", lambda owners: seen.append(set(owners)))

    class _Agent:
        _process_owner_task_ids = ("cron:job:run-1", "sess-rotated")

    ClientLifecycleMixin._close_task_resources(_Agent(), "sess-1")
    assert seen == [{"sess-1", "cron:job:run-1", "sess-rotated"}]


def _fake_cli(tmp_path, body):
    import stat

    script = tmp_path / "browser-use"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_managed_exec_raises_harness_cdp_timeout(tmp_path, monkeypatch):
    state = _State()
    httpd, port = _start(state)
    _managed_env(monkeypatch, port)
    stdin_path = tmp_path / "stdin.txt"
    script = _fake_cli(tmp_path, f"cat > '{stdin_path}'\necho ok\n")
    import tools.browser_use_cli as bu_cli

    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(script)])
    json.loads(bu_cli.browser_exec("print(1)", task_id="t1"))
    httpd.shutdown()
    posted = stdin_path.read_text()
    assert "_hermes_ipc_timeout" in posted and posted.rstrip().endswith("print(1)")
    # The agent works in the front tab: timeouts first, then the tab, then the code.
    assert posted.index("_hermes_ipc_timeout") < posted.index("_hermes_front_tab") < posted.index("print(1)")


def test_managed_front_tab_preamble_activates_and_defaults_switch_tab(monkeypatch):
    """The preamble runs against the real harness helper signatures (no daemon)."""
    import sys
    import types

    import tools.browser_use_cli as bu_cli

    calls = []
    fake = types.ModuleType("browser_harness.helpers")

    def switch_tab(target, activate=False):
        calls.append(("switch", target, activate))
        return "sid"

    def cdp(method, session_id=None, **params):
        calls.append(("cdp", method))

    fake.switch_tab = switch_tab
    fake.cdp = cdp
    pkg = types.ModuleType("browser_harness")
    pkg.helpers = fake
    monkeypatch.setitem(sys.modules, "browser_harness", pkg)
    monkeypatch.setitem(sys.modules, "browser_harness.helpers", fake)
    namespace = {"switch_tab": switch_tab}
    exec(bu_cli._MANAGED_FRONT_TAB_PREAMBLE + "switch_tab('T2')\nswitch_tab('T3', activate=False)\n", namespace)
    # Front and focused at the start; every activating switch is focused too; an explicit
    # activate=False is honoured (no focus call).
    assert calls == [
        ("cdp", "Page.bringToFront"),
        ("switch", "T2", True),
        ("cdp", "Page.bringToFront"),
        ("switch", "T3", False),
    ]
    assert "_hermes_front_tab" not in namespace
    # Helpers that call the module's switch_tab (new_tab, ensure_real_tab) get the patched one.
    assert fake.switch_tab is namespace["switch_tab"]
    # A second call does not wrap twice; a failing daemon never breaks the call.
    calls.clear()
    fake.cdp = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not attached"))
    exec(bu_cli._MANAGED_FRONT_TAB_PREAMBLE + "switch_tab('T4')\n", namespace)
    assert calls == [("switch", "T4", True)]


def test_gate_403_is_explained_and_retried_on_a_fresh_lease(tmp_path, monkeypatch):
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        calls = tmp_path / "calls"
        script = _fake_cli(
            tmp_path,
            # The dead lease's daemon is retired with --reload off-thread; count execs only.
            'case "$1" in --reload) exit 0;; esac\n'
            f"echo x >> '{calls}'\n"
            'case "$BU_CDP_URL" in *tok-1*) echo "browser-harness: permission-blocked: Chrome '
            'is reachable, but the per-session Allow remote debugging popup" >&2; exit 1;; esac\n'
            "echo ok\n",
        )
        import tools.browser_use_cli as bu_cli

        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(script)])
        # First call holds lease-1; then control recycles it.
        state.status_replies["lease-1"] = {"error": "stale"}
        state.acquire_replies.extend(
            [dict(DEFAULT_LEASE), dict(DEFAULT_LEASE, lease_id="lease-2", command_token="tok-2")]
        )
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t1"))
        assert result["success"] is True
        assert len(calls.read_text().split()) == 2
        # A refusal that persists is explained, not blamed on a popup.
        state.status_replies.clear()
        clear_held_leases()
        state.acquire_replies.append(dict(DEFAULT_LEASE))
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t2"))
        assert result["success"] is False
        assert result["stderr"].startswith("managed browser gate refused the lease (HTTP 403)")
    finally:
        httpd.shutdown()


def _fake_control(monkeypatch, acquire):
    """Route with no HTTP: ``_acquire`` is faked, every held lease is live."""
    import tools.browser_control_route as route

    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("BU_AUTOSPAWN", raising=False)
    monkeypatch.setattr(route, "_acquire", acquire)
    monkeypatch.setattr(route, "_lease_alive", lambda control, lease_id: True)
    return route


def _run_parallel(calls):
    """Run ``(env, kwargs)`` resolve calls on threads released together."""
    import threading

    gate = threading.Barrier(len(calls))
    errors = [None] * len(calls)

    def _one(index, env, kwargs):
        gate.wait(timeout=5)
        errors[index] = resolve_managed_cdp(env, **kwargs)

    threads = [
        Thread(target=_one, args=(index, env, kwargs)) for index, (env, kwargs) in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "resolve_managed_cdp deadlocked"
    return errors


def test_parallel_same_key_acquires_once_and_shares_the_lease(monkeypatch):
    import threading
    import time

    count_lock = threading.Lock()
    acquired = []

    def _slow_acquire(control, *, task, mode, identity, qa_targets):
        with count_lock:
            acquired.append(task)
            n = len(acquired)
        time.sleep(0.2)
        return dict(DEFAULT_LEASE, lease_id=f"lease-{n}", command_token=f"tok-{n}")

    route = _fake_control(monkeypatch, _slow_acquire)
    kwargs = {"task_id": "t1", "mode": "research", "identity": "shopping"}
    env_a, env_b = {}, {}
    errors = _run_parallel([(env_a, kwargs), (env_b, kwargs)])
    assert errors == [None, None]
    assert acquired == ["t1"]
    assert env_a[MANAGED_LEASE_ENV] == env_b[MANAGED_LEASE_ENV] == "lease-1"
    assert env_a["BU_CDP_URL"] == env_b["BU_CDP_URL"]
    assert route._key_locks == {}


def test_parallel_different_keys_acquire_concurrently(monkeypatch):
    import threading

    # Both acquires must be in flight at once to pass the barrier; a
    # serialized (global) lock would break it and fail the call.
    both_in_flight = threading.Barrier(2)
    acquired = []

    def _rendezvous_acquire(control, *, task, mode, identity, qa_targets):
        acquired.append(identity)
        try:
            both_in_flight.wait(timeout=5)
        except threading.BrokenBarrierError:
            return {"error": "unavailable", "detail": "acquires were serialized"}
        return dict(DEFAULT_LEASE, lease_id=f"lease-{identity}")

    _fake_control(monkeypatch, _rendezvous_acquire)
    env_a, env_b = {}, {}
    errors = _run_parallel(
        [
            (env_a, {"task_id": "t1", "identity": "shopping"}),
            (env_b, {"task_id": "t1", "identity": "work:default"}),
        ]
    )
    assert errors == [None, None]
    assert sorted(acquired) == ["shopping", "work:default"]
    assert env_a[MANAGED_LEASE_ENV] == "lease-shopping"
    assert env_b[MANAGED_LEASE_ENV] == "lease-work:default"


def test_parallel_same_key_waiter_reacquires_after_a_failed_acquire(monkeypatch):
    import threading
    import time

    count_lock = threading.Lock()
    acquired = []

    def _fail_then_lease(control, *, task, mode, identity, qa_targets):
        with count_lock:
            acquired.append(task)
            n = len(acquired)
        time.sleep(0.1)
        if n == 1:
            return {"error": "busy", "detail": "slot queued"}
        return dict(DEFAULT_LEASE, lease_id="lease-2", command_token="tok-2")

    route = _fake_control(monkeypatch, _fail_then_lease)
    kwargs = {"task_id": "t1", "identity": "shopping"}
    env_a, env_b = {}, {}
    errors = _run_parallel([(env_a, kwargs), (env_b, kwargs)])
    assert len(acquired) == 2
    assert sorted(err is None for err in errors) == [False, True]
    assert any(err and err.startswith("busy:") for err in errors)
    assert (env_a or env_b)[MANAGED_LEASE_ENV] == "lease-2"
    assert route._key_locks == {}


def test_dropped_lease_retires_its_harness_daemon(monkeypatch):
    """2026-10-26: each recycled lease left its per-lease harness daemon running (21 in the gateway)."""
    import tools.browser_control_route as route

    retired = []
    monkeypatch.setattr(route, "_retire_daemon", lambda name: retired.append(name))
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        assert _resolve(port, monkeypatch, task_id="t1")[0] is None
        first = route.held_leases_for_task("t1")[0]["BU_NAME"]
        state.status_replies["lease-1"] = {"error": "stale", "detail": "unknown lease"}
        state.acquire_replies.append(dict(DEFAULT_LEASE, lease_id="lease-2", command_token="tok-2"))
        assert _resolve(port, monkeypatch, task_id="t1")[0] is None
        assert retired == [first]
        second = route.held_leases_for_task("t1")[0]["BU_NAME"]
        assert release_task_leases({"t1"}) == 1
        assert retired == [first, second]
    finally:
        httpd.shutdown()


def test_stop_harness_daemon_only_stops_the_owning_profiles_driven_name(monkeypatch):
    """Lease names are per controller, so two served profiles can share one: retiring profile A's lease
    must leave B's daemon registered and running. Identities have the real daemon_identity shape."""
    import tools.browser_use_cli as cli
    from tools.browser_harness_dirs import daemon_identity

    name = "bu-ctrl-research-1-g-7"

    def identity(home: str) -> tuple:
        return daemon_identity({"BH_RUNTIME_DIR": f"/{home}/rt", "BH_RUNTIME_DIR_SHARED": f"/{home}/rt-shared",
                                "BH_TMP_DIR": f"/{home}/tmp", "BH_TMP_DIR_SHARED": f"/{home}/tmp-shared",
                                "BU_NAME": name})

    a, b = identity("a"), identity("b")
    assert len(a) == 5 and a[-1] == b[-1] == name
    stopped = []
    monkeypatch.setattr(cli, "_reload_daemons", lambda daemons: stopped.extend(daemons))
    monkeypatch.setattr(cli, "_driven_daemons", {a: "home-a", b: "home-b"})
    cli.stop_harness_daemon("bu-ctrl-shopping-g-9", "home-a")  # not driven: no-op
    cli.stop_harness_daemon(name, "home-a")
    cli.stop_harness_daemon(name, "home-a")  # idempotent
    assert stopped == [a]
    assert cli._driven_daemons == {b: "home-b"}


def test_retire_daemon_passes_the_callers_profile(monkeypatch):
    import threading

    import tools.browser_control_route as route
    import tools.browser_use_cli as cli

    calls, done = [], threading.Event()
    monkeypatch.setattr(cli, "stop_harness_daemon", lambda name, home: (calls.append((name, home)), done.set()))
    monkeypatch.setattr("hermes_constants.hermes_home_key", lambda: "home-a")
    route._retire_daemon("bu-ctrl-research-1-g-7")
    route._retire_daemon("not-a-lease")  # never a daemon of ours
    assert done.wait(5)
    assert calls == [("bu-ctrl-research-1-g-7", "home-a")]


def test_registry_handler_passes_managed_kwargs(monkeypatch):
    """The v0.23 handler wraps browser_exec in record_browser_call; dropping mode/identity there would run a
    work:<org> call in a research slot (B2b)."""
    import tools.browser_use_cli as bu_cli
    from tools.registry import registry

    seen = {}
    monkeypatch.setattr(bu_cli, "browser_exec", lambda **kw: seen.update(kw) or "{}")
    monkeypatch.setattr("hermes_cli.observability.shared_metrics_loop.record_browser_call",
                        lambda call, backend: call())
    entry = registry.get_entry("browser_exec") if hasattr(registry, "get_entry") else registry._tools["browser_exec"]
    entry.handler({"code": "print(1)", "mode": "qa", "identity": "work:acme", "qa_targets": ["app.example"],
                   "on_dialog": "dismiss", "bridge": "b1", "action": "verify"}, task_id="t1")
    assert {k: seen[k] for k in ("mode", "identity", "qa_targets", "on_dialog", "bridge", "action", "task_id")} == {
        "mode": "qa", "identity": "work:acme", "qa_targets": ["app.example"], "on_dialog": "dismiss",
        "bridge": "b1", "action": "verify", "task_id": "t1"}
