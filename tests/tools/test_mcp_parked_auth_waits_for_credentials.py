"""An OAuth server parked because the authorization server refused its token refresh as a dead
grant (RFC 6749 §5.2 ``invalid_grant`` / ``invalid_client`` / ``unauthorized_client``) logs
"parking until credentials change", and the timed self-probe honours that: with the same
credentials it can only replay the dead grant. Any credential change the server reads (token
file, client file, config, a ``${VAR}`` in the owning profile's ``.env``) wakes it AND is adopted.
Every other refusal (a WAF page, a proxy, a throttle, a 5xx) says nothing about the grant and keeps
the timed recovery.

The real ``MCPServerTask.run``, OAuth provider and MCP SDK talk to a loopback server that is both
the authorization server and the MCP endpoint; the subclass below only counts attempts and wakes.
"""

import asyncio
import contextlib
import contextvars
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

pytest.importorskip("mcp.client.auth.oauth2", reason="MCP SDK required for OAuth support")

from tests.waiting import async_wait_until

DEAD_GRANT = (400, {"error": "invalid_grant", "error_description": "refresh token revoked"})


class _Server(ThreadingHTTPServer):
    """``token``: (status, body) for every token POST; ``resource``: the status ``/mcp`` answers.
    ``refreshes`` records each token POST (form fields plus the User-Agent)."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.base = f"http://127.0.0.1:{self.server_address[1]}"
        self.token, self.resource = DEAD_GRANT, 403
        self.refreshes: list[dict] = []
        self.hold_token = threading.Event()
        self.hold_token.set()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body=b"", content_type="application/json", headers=()):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        server = self.server
        if self.path.startswith("/.well-known/oauth-authorization-server"):
            return self._reply(200, {
                "issuer": server.base, "authorization_endpoint": f"{server.base}/authorize",
                "token_endpoint": f"{server.base}/token", "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"]})
        if self.path.startswith("/.well-known/"):
            return self._reply(404, {})
        return self._resource()

    def do_POST(self):
        server = self.server
        if self.path == "/token":
            form = {k: v[0] for k, v in parse_qs(self._read_body().decode()).items()}
            form["user-agent"] = self.headers.get("User-Agent")
            server.refreshes.append(form)
            server.hold_token.wait(10)
            status, body = server.token
            return self._reply(status, body, "application/json" if isinstance(body, dict) else "text/html")
        self._read_body()
        return self._resource()

    def _resource(self):
        status = self.server.resource
        headers = [("WWW-Authenticate", "Bearer")] if status == 401 else []
        return self._reply(status, {"error": "nope"} if status != 401 else b"", headers=headers)


@pytest.fixture
def auth_server():
    server = _Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.hold_token.set()
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _fast_park(monkeypatch):
    from tools import mcp_tool, mcp_tool_server_run
    from tools.mcp_oauth_manager import reset_manager_for_tests

    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 0.02)
    monkeypatch.setattr(mcp_tool, "_PARKED_PROBE_BACKOFF_MAX", 0.05)
    monkeypatch.setattr(mcp_tool_server_run, "_jittered", lambda seconds: 0.0)
    monkeypatch.setattr("tools.mcp_oauth._stdin_is_console", lambda: False)  # never a browser
    reset_manager_for_tests()
    yield
    reset_manager_for_tests()
    import tools.mcp_oauth as mod
    mod._assigned_cimd_ports.clear()
    for port in list(mod._reserved_sockets):
        sock = mod._reserved_sockets.pop(port, None)
        if sock is not None:
            sock.close()


def _write_config(home, server, user_agent="ua-1", oauth=""):
    (home / "config.yaml").write_text(
        "mcp_servers:\n  srv:\n"
        f"    url: {server.base}/mcp\n    auth: oauth\n    connect_timeout: 5\n"
        f"    oauth:\n      user_agent: '{user_agent}'\n{oauth}", encoding="utf-8")


def _write_tokens(home, server, refresh_token="R1"):
    path = home / "mcp-tokens" / "srv.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({  # expired: the first request refreshes
        "access_token": f"access-{refresh_token}", "token_type": "Bearer", "expires_in": 3600,
        "refresh_token": refresh_token, "expires_at": time.time() - 60,
        "hermes_issuer": server.base}), encoding="utf-8")


def _write_client(home, client_id="client-1"):
    path = home / "mcp-tokens" / "srv.client.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "client_id": client_id, "redirect_uris": ["http://127.0.0.1:12345/callback"],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none"}), encoding="utf-8")


def _profile(home, server, **config):
    home.mkdir(parents=True, exist_ok=True)
    _write_config(home, server, **config)
    _write_tokens(home, server)
    _write_client(home)
    return home


def _run(scenario, *, scope=contextlib.nullcontext):
    """Run the real task for ``srv`` (config read under *scope*) beside ``scenario(task, state)``;
    ``state`` counts transport attempts (``calls``) and timed self-probe wakes (``wakes``)."""
    from tools import mcp_tool, mcp_tool_config

    state = {"calls": 0, "wakes": 0}

    class _Task(mcp_tool.MCPServerTask):
        async def _wait_for_reconnect_or_shutdown(self, timeout=None):
            outcome = await super()._wait_for_reconnect_or_shutdown(timeout)
            state["wakes"] += outcome == "self-probe"
            return outcome

        async def _run_http(self, config):
            state["calls"] += 1
            return await super()._run_http(config)

    async def _main():
        with scope():
            task = _Task("srv")
            run_task = asyncio.ensure_future(task.run(mcp_tool_config._load_mcp_config()["srv"]))
        try:
            await scenario(task, state)
        finally:
            task._shutdown_event.set()
            task._reconnect_event.set()
            await asyncio.wait_for(run_task, timeout=15)

    asyncio.run(_main())


async def _stays_parked(state, server, wakes=5):
    """Wait for *wakes* more timed self-probes once parked; none may reach the authorization server."""
    await async_wait_until(lambda: state["wakes"], what="the server never parked")
    calls, refreshes, target = state["calls"], len(server.refreshes), state["wakes"] + wakes
    await async_wait_until(lambda: state["wakes"] >= target, what="the parked server never self-probed")
    assert (state["calls"], len(server.refreshes)) == (calls, refreshes), \
        "the self-probe replayed a grant the authorization server refused"


async def _next_refresh(server, what):
    seen = len(server.refreshes)
    await async_wait_until(lambda: len(server.refreshes) > seen, what=what)
    return server.refreshes[-1]


@pytest.mark.parametrize("token, resource", [
    (DEAD_GRANT, 403), (DEAD_GRANT, 503), ((400, {"error": "unauthorized_client"}), 401),
], ids=["invalid_grant-then-403", "invalid_grant-then-503", "unauthorized_client-then-401"])
def test_a_dead_grant_parks_until_credentials_change_whatever_the_resource_answers(
        tmp_path, monkeypatch, auth_server, token, resource):
    """The SDK still sends the request after a refused refresh; a 403 there is not an auth
    error and a 503 is transient, but neither may unpark a server whose grant is dead."""
    home = _profile(tmp_path / "home", auth_server)
    monkeypatch.setenv("HERMES_HOME", str(home))
    auth_server.token, auth_server.resource = token, resource

    async def scenario(task, state):
        await _stays_parked(state, auth_server)
        assert state["calls"] == 1

    _run(scenario)


@pytest.mark.parametrize("oauth", ["      client_id: client-1\n", "      client_id: client-1\n      flow: device\n"],
                         ids=["pre-registered", "pre-registered-device"])
def test_a_pre_registered_client_does_not_wake_its_own_park(tmp_path, monkeypatch, auth_server, oauth):
    """Provider setup rewrites the pre-registered client file on every build; that is not a
    credential change, or each timed wake (and each discovery) would evict, rebuild and replay
    the dead grant."""
    home = tmp_path / "home"
    _profile(home, auth_server, oauth=oauth)
    monkeypatch.setenv("HERMES_HOME", str(home))
    auth_server.token, auth_server.resource = DEAD_GRANT, 401

    async def scenario(task, state):
        await async_wait_until(lambda: task._auth_park is not None, what="the server never parked")
        assert not task._credentials_changed_since(task._auth_park), \
            "discovery would wake the park on its own provider setup"
        await _stays_parked(state, auth_server)
        assert state["calls"] == 1

    _run(scenario)


@pytest.mark.parametrize("token", [
    (403, "<html><title>Request blocked</title></html>"),  # a WAF/CDN in front of the token endpoint
    (407, "<html>Proxy Authentication Required</html>"),
    (429, {"error": "slow_down"}),
    (503, {"error": "temporarily_unavailable"}),
    (400, {"error": "invalid_request"}),  # not a verdict on the grant
    (403, {"error": "invalid_grant"}),    # the right word on the wrong status
], ids=["waf-403", "proxy-407", "throttle-429", "outage-503", "invalid_request", "invalid_grant-on-403"])
def test_a_refusal_that_says_nothing_about_the_grant_keeps_the_timed_recovery(
        tmp_path, monkeypatch, auth_server, token):
    """The resource then answers 401 and the background authorization step fails: an auth park,
    but the refresh token on disk may still be good, and only the timed probe can find out."""
    home = _profile(tmp_path / "home", auth_server)
    monkeypatch.setenv("HERMES_HOME", str(home))
    auth_server.token, auth_server.resource = token, 401

    async def scenario(task, state):
        await async_wait_until(lambda: state["calls"] >= 3, what="the timed recovery stopped")

    _run(scenario)


def _change_token(home, server):
    _write_tokens(home, server, "R2")
    return "refresh_token", "R2"


def _change_client(home, server):
    _write_client(home, "client-2")
    return "client_id", "client-2"


def _change_config(home, server):
    _write_config(home, server, user_agent="ua-2")
    return "user-agent", "ua-2"


@pytest.mark.parametrize("change", [_change_token, _change_client, _change_config],
                         ids=["token-file", "client-file", "config"])
def test_a_credential_change_wakes_the_parked_server_and_is_adopted(tmp_path, monkeypatch, auth_server, change):
    """``hermes mcp login`` from another process, a new client registration or an edited
    ``oauth:`` block: the next probe runs, and with the NEW value (the cached provider would
    otherwise keep the old client, user agent or oauth settings: it is rebuilt only for a new URL)."""
    home = _profile(tmp_path / "home", auth_server)
    monkeypatch.setenv("HERMES_HOME", str(home))

    async def scenario(task, state):
        await _stays_parked(state, auth_server)
        field, value = change(home, auth_server)
        refresh = await _next_refresh(auth_server, "the credential change never woke the probe")
        assert refresh[field] == value

    _run(scenario)


@contextlib.contextmanager
def _profile_scope(home):
    """A routed profile's runtime scope as the gateway binds it: home override plus a secret
    scope built NOW (the run task keeps this copy for its life)."""
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(str(home))
    token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(token)
        reset_hermes_home_override(home_token)


def test_the_owning_profiles_env_edit_wakes_and_is_adopted_another_profiles_does_neither(
        tmp_path, monkeypatch, auth_server):
    """``oauth.user_agent: ${SRV_UA}`` resolves from the OWNING profile's ``.env``. The run task's
    copied secret mapping is a connect-time snapshot, so the check rebuilds the owner's scope;
    discovery runs the same check from its caller's context, which may be another profile's."""
    from agent.secret_scope import set_multiplex_active

    owner, other = (_profile(tmp_path / name, auth_server, user_agent="${SRV_UA}") for name in ("owner", "other"))
    (owner / ".env").write_text("SRV_UA=ua-1\n", encoding="utf-8")
    (other / ".env").write_text("SRV_UA=ua-other\n", encoding="utf-8")

    async def scenario(task, state):
        await _stays_parked(state, auth_server)
        assert auth_server.refreshes[-1]["user-agent"] == "ua-1"
        foreign = contextvars.Context()  # discovery's caller: no scope of the owner's
        (other / ".env").write_text("SRV_UA=ua-other-2\n", encoding="utf-8")
        _write_tokens(other, auth_server, "R-other")
        await _stays_parked(state, auth_server)
        assert not foreign.run(task._credentials_changed_since, task._auth_park)

        (owner / ".env").write_text("SRV_UA=ua-2\n", encoding="utf-8")
        assert foreign.run(task._credentials_changed_since, task._auth_park)
        refresh = await _next_refresh(auth_server, "the owner's .env edit never woke the probe")
        assert refresh["user-agent"] == "ua-2"

    set_multiplex_active(True)
    try:
        _run(scenario, scope=lambda: _profile_scope(owner))
    finally:
        set_multiplex_active(False)


def test_a_login_during_the_refused_attempt_still_wakes_it(tmp_path, monkeypatch, auth_server):
    """The gate compares against the credentials the failed attempt STARTED with: a snapshot taken
    at park time would already hold the new login and never see it change."""
    home = _profile(tmp_path / "home", auth_server)
    monkeypatch.setenv("HERMES_HOME", str(home))
    auth_server.hold_token.clear()

    async def scenario(task, state):
        await async_wait_until(lambda: auth_server.refreshes, what="the first refresh never ran")
        _write_tokens(home, auth_server, "R2")
        auth_server.hold_token.set()
        # The attempt itself may already re-read the new file; what matters is that the park wakes.
        await async_wait_until(lambda: state["calls"] >= 2, what="a login during the refused attempt never woke it")
        assert auth_server.refreshes[-1]["refresh_token"] == "R2"

    _run(scenario)


def test_an_explicit_reconnect_retries_and_a_network_failure_after_it_keeps_probing(
        tmp_path, monkeypatch, auth_server):
    """``hermes mcp login`` / a manual refresh signals a reconnect: it always runs. When that attempt
    then fails on the network, the park is about the network, and its timed probe runs."""
    home = _profile(tmp_path / "home", auth_server)
    monkeypatch.setenv("HERMES_HOME", str(home))

    async def scenario(task, state):
        await _stays_parked(state, auth_server)
        calls, wakes = state["calls"], state["wakes"]
        task._reconnect_event.set()
        await async_wait_until(lambda: state["calls"] > calls and state["wakes"] > wakes,
                               what="the reconnect never ran and parked again")
        await _stays_parked(state, auth_server)  # it replayed the dead grant: gated again
        auth_server.shutdown()  # the authorization server and the MCP endpoint go away
        auth_server.server_close()
        calls = state["calls"]
        task._reconnect_event.set()
        await async_wait_until(lambda: state["calls"] >= calls + 3, what="the network park stopped probing")

    _run(scenario)


@pytest.mark.parametrize("status, body, error", [
    (400, {"error": "invalid_grant"}, "invalid_grant"),
    (401, {"error": "invalid_client"}, "invalid_client"),
    (400, {"error": "unauthorized_client"}, "unauthorized_client"),
    (400, {"error": "invalid_scope"}, None),
    (400, b"<html>Bad Request</html>", None),
    (400, b'["invalid_grant"]', None),
    (400, {"error": ["invalid_grant"]}, None),
    (403, {"error": "invalid_grant"}, None),
    (500, {"error": "invalid_grant"}, None),
])
def test_only_an_rfc6749_dead_grant_error_is_a_verdict(status, body, error):
    from tools.mcp_oauth_provider import _refusal_error
    from tools.mcp_tool import sdk_httpx

    httpx = sdk_httpx()
    content = json.dumps(body).encode() if isinstance(body, dict) else body
    response = httpx.Response(status, content=content, request=httpx.Request("POST", "http://127.0.0.1/token"))
    assert asyncio.run(_refusal_error(response)) == error


def test_a_login_in_the_same_attempt_ends_the_refusal(tmp_path, monkeypatch, auth_server):
    """The refresh was refused, then the user logged in through the browser flow and the session
    connected: a network blip after that parks as a dead grant only if the stale refusal survived."""
    from mcp.shared.auth import OAuthToken
    from tools.mcp_oauth_manager import get_manager

    home = _profile(tmp_path / "home", auth_server)
    monkeypatch.setenv("HERMES_HOME", str(home))
    provider = get_manager().get_or_build_provider("srv", f"{auth_server.base}/mcp", {})
    provider._hermes_refresh_refused = "invalid_grant"
    asyncio.run(provider._store_tokens(OAuthToken(access_token="A2", token_type="Bearer", refresh_token="R2")))
    assert get_manager().take_refresh_refusal("srv") is None
