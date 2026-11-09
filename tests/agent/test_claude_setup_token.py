"""dlz: owner-authenticated `claude setup-token` intake (handoff acceptance tests 1-7).

Everything here is SYNTHETIC: a fake native CLI on a real PTY with a real loopback listener, fake
state/code/token canaries, and a temp sink. None of it is a real Claude authorization.
"""

import http.server
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent import claude_setup_token as cst

STATE = "SYNTHSTATE-c4n4ry-state"
CODE = "SYNTHCODE-c4n4ry-code"
TOKEN = "sk-ant-oat01-SYNTHETIC-c4n4ry-token-" + "x" * 48
CANARIES = (STATE, CODE, TOKEN)

FAKE_CLI = r'''
import http.server, os, sys, time
mode, record = sys.argv[1], sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        with open(record, "a") as fh:
            fh.write(self.path + "\n")
        if mode == "hang":
            time.sleep(30)
        self.send_response(302); self.send_header("Location", "https://platform.claude.com/oauth/code/success")
        self.end_headers()
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
print("Browser didn't open? Use the url below to sign in", flush=True)
print("\x1b[1mhttps://" + ("claude.com.evil.io" if mode == "evil" else "claude.com") + "/cai/oauth/authorize?code=true&client_id=synthetic-client&response_type=code"
      "&redirect_uri=http%3A%2F%2Flocalhost%3A" + str(port) + "%2Fcallback&scope=user%3Ainference"
      "&code_challenge=SYNTHCHALLENGE&code_challenge_method=S256&state=" + os.environ["FAKE_STATE"] + "\x1b[0m",
      flush=True)
if mode == "die":
    sys.exit(3)
srv.handle_request()
if mode == "fail":
    sys.exit(1)
tok = os.environ["FAKE_TOKEN"]
print("Your OAuth token (valid for 1 year):\n", flush=True)
print(tok[:30] if mode == "partial" else tok, end="" if mode == "partial" else "\n", flush=True)
if mode == "linger":
    time.sleep(60)
'''


class MemSink:
    def __init__(self, fail_store=False, mismatch=False):
        self.items, self.fail_store, self.mismatch = {}, fail_store, mismatch

    def store_pending(self, flow_id, secret):
        if self.fail_store:
            raise OSError("sink down")
        self.items[f"pending-{flow_id}"] = secret
        return f"pending-{flow_id}"

    def readback_matches(self, handle, secret):
        return not self.mismatch and self.items.get(handle) == secret


@pytest.fixture
def env(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLI)
    record = tmp_path / "received.txt"
    record.write_text("")
    monkeypatch.setenv("FAKE_STATE", STATE)
    monkeypatch.setenv("FAKE_TOKEN", TOKEN)

    def make(mode="ok", sink=None, **kw):
        kw.setdefault("listener_check", lambda port, pgid: True)
        kw.setdefault("url_wait_s", 20)
        return cst.Broker(argv=[sys.executable, str(script), mode, str(record)],
                          sink_factory=(lambda: sink) if sink else (lambda: cst.FilePendingSink(tmp_path / "sink")),
                          **kw)

    def received():
        return [line for line in record.read_text().splitlines() if line]

    return make, received, tmp_path


def _callback(flow, **override):
    q = {"code": CODE, "state": STATE, **override}
    query = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
    return f"http://localhost:{flow.native.redirect_port}{flow.native.redirect_path}?{query}"


def _start(broker, profile="/p/a", session="s1", owner="owner"):
    flow = broker.start(profile=profile, session_id=session, owner=owner)
    assert flow.state == cst.AWAITING, flow.reason
    return flow


def _settled(flow, timeout=20):
    assert flow.finished.wait(timeout), flow.state
    return flow


# 1. Positive integration -------------------------------------------------------------------------

def test_positive_flow_delivers_once_and_stores_privately(env):
    make, received, tmp = env
    broker = make()
    flow = _start(broker)
    view = broker.view(flow.handoff_id, profile="/p/a", principal="owner")
    assert view["authorize_url"].startswith("https://claude.com/cai/oauth/authorize?")
    out = broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
    assert out["state"] in (cst.DELIVERED, cst.STORED)
    _settled(flow)
    assert flow.state == cst.STORED and flow.readback_ok and flow.token_issued
    assert received() == [f"/callback?code={CODE}&state={STATE}"]
    stored = tmp / "sink" / f"{flow.sink_handle}.token"
    assert stored.read_text() == TOKEN and (stored.stat().st_mode & 0o777) == 0o600
    model = json.dumps(flow.model_view())
    assert not any(c in model for c in CANARIES) and "authorize" not in model
    assert flow.model_view()["installed"] is False


def test_cli_lingering_after_a_complete_token_is_stopped_and_token_kept(env, monkeypatch):
    make, _, _ = env
    monkeypatch.setattr(cst, "_TOKEN_LINGER_S", 0.5)
    broker = make("linger")
    flow = _start(broker)
    broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
    _settled(flow)
    assert flow.state == cst.STORED and flow.readback_ok and flow.proc.returncode is not None


# 2. Owner/profile/session isolation, expiry ------------------------------------------------------

def test_other_owner_profile_session_or_handoff_cannot_touch_flow(env):
    make, received, _ = env
    broker = make()
    flow = _start(broker)
    with pytest.raises(cst.IntakeRejected) as exc:  # only the owner fixed at start, even first
        broker.view(flow.handoff_id, profile="/p/a", principal="intruder")
    assert exc.value.code == "not_owner"
    for kwargs, code in (({"profile": "/p/b", "principal": "owner"}, "not_found"),
                         ({"profile": "/p/a", "principal": "intruder"}, "not_owner")):
        with pytest.raises(cst.IntakeRejected) as exc:
            broker.submit(flow.handoff_id, _callback(flow), **kwargs)
        assert exc.value.code == code
    for handoff in (flow.flow_id, "x" * 32):  # the model's flow handle is not a handoff
        with pytest.raises(cst.IntakeRejected):
            broker.view(handoff, profile="/p/a", principal="owner")
    for kwargs in ({"profile": "/p/a", "session_id": "s2"}, {"profile": "/p/b", "session_id": "s1"}):
        for op in (broker.status, broker.cancel):
            with pytest.raises(cst.IntakeRejected):
                op(flow.flow_id, **kwargs)
    flow.deadline = time.time() - 1
    with pytest.raises(cst.IntakeRejected):
        broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
    assert flow.state == cst.EXPIRED and received() == []


# 3. Callback integrity / SSRF --------------------------------------------------------------------

def _attacker_listener():
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


def test_bad_callbacks_never_deliver_or_fetch(env):
    make, received, _ = env
    broker = make()
    flow = _start(broker)
    srv, hits = _attacker_listener()
    evil, port = srv.server_address[1], flow.native.redirect_port
    good_q = f"code={CODE}&state={STATE}"
    bad = [
        _callback(flow, state="wrong"),
        _callback(flow, state=None),
        f"http://localhost:{evil}/callback?{good_q}",                 # wrong port (attacker listener)
        f"http://localhost:{port}/other?{good_q}",                    # wrong path
        f"http://127.0.0.2:{port}/callback?{good_q}",                 # wrong host
        f"http://169.254.169.254:{port}/callback?{good_q}",
        f"https://localhost:{port}/callback?{good_q}",
        f"http://localhost:{port}@127.0.0.1:{evil}/callback?{good_q}",  # userinfo
        f"http://u@localhost:{port}/callback?{good_q}",
        _callback(flow) + "#frag",
        _callback(flow) + f"&code={CODE}",                             # duplicate code
        _callback(flow) + f"&state={STATE}",                           # duplicate state
        _callback(flow, code="%zz"),                                  # malformed escape
        _callback(flow, redirect_uri=f"http%3A%2F%2F127.0.0.1%3A{evil}%2F"),  # attacker redirect
        f"http://localhost:{port}/%63allback?{good_q}",
    ]
    for raw in bad:
        with pytest.raises(cst.IntakeRejected):
            broker.submit(flow.handoff_id, raw, profile="/p/a", principal="owner")
    assert flow.state == cst.AWAITING and received() == [] and hits == []
    # Negative control: a generic "fetch what the owner pasted" relay would have hit the attacker.
    import urllib.request
    urllib.request.urlopen(f"http://127.0.0.1:{evil}/callback?{good_q}", timeout=5).read()
    assert hits, "the attacker listener must be able to observe an unsafe relay"
    srv.shutdown()
    broker.cancel(flow.flow_id, profile="/p/a", session_id="s1")


@pytest.mark.parametrize("url", [
    "http://claude.com/cai/oauth/authorize?{q}",
    "https://claude.com.evil.io/cai/oauth/authorize?{q}",
    "https://evil.io/claude.com/oauth/authorize?{q}",
    "https://user@claude.com/cai/oauth/authorize?{q}",
    "https://claude.com/cai/oauth/authorize?{q}&state=dup",
    "https://claude.com/cai/oauth/authorize?{q_plain}",
])
def test_authorize_url_allowlist(url):
    base = ("client_id=c&response_type=code&scope=user%3Ainference&code_challenge=x&state=s"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A40000%2Fcallback")
    with pytest.raises(ValueError):
        cst.parse_authorize_url(url.format(q=base + "&code_challenge_method=S256",
                                           q_plain=base + "&code_challenge_method=plain"))
    assert cst.parse_authorize_url("https://claude.com/cai/oauth/authorize?" + base
                                   + "&code_challenge_method=S256").redirect_port == 40000
    for redirect in ("http%3A%2F%2Fevil.io%3A40000%2Fcb", "https%3A%2F%2Flocalhost%3A40000%2Fcb"):
        with pytest.raises(ValueError):
            cst.parse_authorize_url("https://claude.com/cai/oauth/authorize?" + base.split("&redirect_uri")[0]
                                    + "&code_challenge_method=S256&redirect_uri=" + redirect)


# 4. Lifecycle and concurrency --------------------------------------------------------------------

def test_concurrent_duplicate_submits_forward_once(env):
    make, received, _ = env
    broker = make()
    flow = _start(broker)
    results, barrier = [], threading.Barrier(4)

    def go():
        barrier.wait()
        try:
            results.append(broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner"))
        except cst.IntakeRejected as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    _settled(flow)
    assert len(received()) == 1 and sum(isinstance(r, dict) for r in results) == 1
    assert results.count("not_awaiting") == 3
    assert flow.state == cst.STORED


def test_dead_cli_port_reuse_and_restart_fail_closed(env):
    make, received, _ = env
    broker = make("die")
    flow = broker.start(profile="/p/a", session_id="s1", owner="owner")
    _settled(flow)
    if flow.native is None:  # settled before we could submit: the handoff is gone
        assert flow.state == cst.FAILED
    with pytest.raises(cst.IntakeRejected):
        broker.submit(flow.handoff_id, "http://localhost:1/callback", profile="/p/a", principal="owner")
    # A listener the CLI does not own (e.g. a reused port), or one that cannot be attributed on this
    # platform, is refused even while the CLI lives.
    for verdict in (False, None):
        broker = make(listener_check=lambda port, pgid, v=verdict: v)
        flow = _start(broker)
        with pytest.raises(cst.IntakeRejected) as exc:
            broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
        assert exc.value.code == "cli_gone" and received() == []
    # Restart: a fresh broker knows no earlier handoff.
    with pytest.raises(cst.IntakeRejected):
        make().submit(flow.handoff_id, "http://localhost:1/callback", profile="/p/a", principal="owner")
    assert received() == []


def test_uncertain_delivery_is_not_replayed_and_cancel_is_scoped(env):
    make, received, _ = env
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        broker = make("hang", delivery_timeout_s=1.0)
        flow = _start(broker)
        out = broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
        assert out["state"] == cst.DELIVERY_UNKNOWN
        with pytest.raises(cst.IntakeRejected):
            broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
        broker.cancel(flow.flow_id, profile="/p/a", session_id="s1")
        _settled(flow)
        assert flow.state == cst.CANCELLED and flow.reason == "cancelled_after_delivery"
        assert len(received()) == 1 and flow.proc.returncode is not None
        assert bystander.poll() is None  # cancel touched only the flow's own process group
        with pytest.raises(cst.IntakeRejected):  # cancelled: the handoff is gone
            broker.view(flow.handoff_id, profile="/p/a", principal="owner")
    finally:
        bystander.kill()


def test_cancel_while_connecting_sends_nothing(env, monkeypatch):
    make, received, _ = env
    broker = make()
    flow = _start(broker)
    real_connect = cst.http.client.HTTPConnection.connect

    def connect_then_cancel(conn):
        real_connect(conn)
        t = threading.Thread(target=broker.cancel, args=(flow.flow_id,), kwargs={"profile": "/p/a", "session_id": "s1"})
        t.start()
        t.join(10)

    sent = []
    real_put = cst.http.client.HTTPConnection.putrequest
    monkeypatch.setattr(cst.http.client.HTTPConnection, "connect", connect_then_cancel)
    monkeypatch.setattr(cst.http.client.HTTPConnection, "putrequest",
                        lambda conn, *a, **k: (sent.append(a), real_put(conn, *a, **k))[1])
    out = broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
    _settled(flow)
    assert out["state"] == cst.CANCELLED and flow.state == cst.CANCELLED
    assert sent == [] and received() == []  # not one request byte after the cancel won


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="listener attribution reads /proc (Linux)")
def test_linux_listener_attribution_is_real(env):
    make, received, _ = env
    broker = make(listener_check=cst._linux_listener_in_group)
    flow = _start(broker)
    assert cst._linux_listener_in_group(flow.native.redirect_port, flow.pgid) is True
    assert cst._linux_listener_in_group(flow.native.redirect_port, os.getpgid(0)) is False
    broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
    _settled(flow)
    assert flow.state == cst.STORED and len(received()) == 1


# 5. Secret containment ---------------------------------------------------------------------------

def test_canaries_absent_from_logs_tool_envelopes_argv_and_errors(env, caplog, monkeypatch):
    make, _, _ = env
    caplog.set_level(logging.DEBUG)
    broker = make()
    cst.install_broker(broker)
    from tools import claude_setup_token_tool as tool
    monkeypatch.setattr(tool, "_cfg", lambda: {"enabled": True, "handoff_base_url": "https://dash.example"})
    try:
        started = json.loads(tool.claude_setup_token({"action": "start"}, session_id="s1"))
        flow_id = started["flow"]
        flow = broker._flows[flow_id]
        # dlz: the allowlisted authorize URL (with its state) is shown to Danil by policy; nothing else.
        assert started["authorize_url"] == flow.native.authorize_url
        assert CODE not in json.dumps(started) and TOKEN not in json.dumps(started)
        envelopes = [json.dumps({k: v for k, v in started.items() if k != "authorize_url"})]
        rejected = cst.control_verb({"action": "submit", "handoff": flow.handoff_id, "profile": flow.profile,
                                     "principal": "session-token", "callback": _callback(flow, state="nope")}, "uid:1")
        envelopes.append(json.dumps(rejected))
        envelopes.append(json.dumps(cst.control_verb(
            {"action": "submit", "handoff": flow.handoff_id, "profile": flow.profile, "principal": "session-token",
             "callback": _callback(flow)}, "uid:1")))
        _settled(flow)
        envelopes.append(tool.claude_setup_token({"action": "status", "flow": flow_id}, session_id="s1"))
        envelopes.append(json.dumps(cst.control_verb({"action": "view", "handoff": flow.handoff_id, "profile": flow.profile,
                                                      "principal": "session-token"}, "uid:1")))
    finally:
        cst.install_broker(cst.Broker())
    assert json.loads(envelopes[-2])["state"] == cst.STORED
    assert rejected == {"ok": False, "error": "callback_wrong_state"}
    surfaces = envelopes + [caplog.text, " ".join(map(str, flow.proc.args)), repr(flow)]
    for text in surfaces:
        assert not any(c in text for c in CANARIES), text
    assert started["handoff_url"].startswith("https://dash.example/claude-code/setup-token#h=")
    assert not flow.output  # PTY bytes dropped once settled
    assert set(tool.CLAUDE_SETUP_TOKEN_SCHEMA["parameters"]["properties"]) == {"action", "flow", "callback"}


def test_chat_pasted_callback_submit_delivers_once_and_never_echoes(env, caplog, monkeypatch):
    """dlz: owner policy 2026-11-09: Danil may paste the final localhost callback in chat; the tool's
    submit goes through the same strict parse and one-shot delivery, scoped to the starting session."""
    make, received, _ = env
    caplog.set_level(logging.DEBUG)
    broker = make()
    cst.install_broker(broker)
    from tools import claude_setup_token_tool as tool
    monkeypatch.setattr(tool, "_cfg", lambda: {"enabled": True, "handoff_base_url": "https://dash.example"})
    call = lambda session="s1", **a: json.loads(tool.claude_setup_token(a, session_id=session))
    try:
        started = call(action="start")
        flow_id = started["flow"]
        flow = broker._flows[flow_id]
        good = _callback(flow)
        assert started["authorize_url"].startswith("https://claude.com/cai/oauth/authorize?")
        assert call(action="status", flow=flow_id)["authorize_url"] == started["authorize_url"]
        assert "paste" in started["next"] and "in chat" in started["next"] and "handoff_url" in started
        outs = [call(action="submit", flow=flow_id, callback=good, session="other"),
                call(action="submit", flow=flow_id, callback=_callback(flow, state="nope")),
                call(action="submit", flow=flow_id, callback=good.replace("localhost", "127.0.0.1")),
                call(action="submit", flow=flow_id, callback=good)]
        outs.append(call(action="submit", flow=flow_id, callback=good))  # replay
        _settled(flow)
        outs.append(call(action="status", flow=flow_id))
    finally:
        cst.install_broker(cst.Broker())
    assert [o.get("error") for o in outs[:3]] == ["claude_setup_token: not_found", "claude_setup_token: callback_wrong_state",
                                                  "claude_setup_token: callback_wrong_redirect"]
    assert outs[3]["state"] in (cst.DELIVERED, cst.STORED) and "error" in outs[4]
    assert outs[-1]["state"] == cst.STORED and received() == [f"/callback?code={CODE}&state={STATE}"]
    for text in [json.dumps(o) for o in outs] + [caplog.text]:
        assert not any(c in text for c in CANARIES) and "localhost:" not in text, text
    assert "authorize_url" not in outs[-1]  # no longer awaiting
    # Allowlist still enforced: a CLI printing a look-alike host never surfaces a URL.
    cst.install_broker(make("evil", url_wait_s=3))
    try:
        evil = call(action="start")
    finally:
        cst.install_broker(cst.Broker())
    assert evil["state"] == cst.FAILED and "authorize_url" not in evil and "evil" not in json.dumps(evil)


# 6. Credential recovery --------------------------------------------------------------------------

@pytest.mark.parametrize("mode,sink,reason", [
    ("partial", None, "no_complete_token"),
    ("fail", None, "no_complete_token"),
    ("ok", MemSink(fail_store=True), "sink_failed"),
    ("ok", MemSink(mismatch=True), "readback_mismatch"),
])
def test_incomplete_token_or_sink_failure_never_reports_success(env, tmp_path, mode, sink, reason):
    make, _, _ = env
    runtime = tmp_path / "runtime-credential.json"
    runtime.write_text('{"previous": "kept"}')
    broker = make(mode, sink=sink)
    flow = _start(broker)
    broker.submit(flow.handoff_id, _callback(flow), profile="/p/a", principal="owner")
    _settled(flow)
    view = flow.model_view()
    assert flow.state == cst.FAILED and flow.reason == reason
    assert view["readback_ok"] is False and view["installed"] is False
    assert runtime.read_text() == '{"previous": "kept"}'
    if mode != "ok":
        assert not (tmp_path / "sink").exists() or not list((tmp_path / "sink").iterdir())


# 7. Existing defenses ----------------------------------------------------------------------------

def test_tool_is_off_by_default_and_vault_boundaries_unchanged():
    from tools import claude_setup_token_tool as tool
    assert tool.check_available() is False  # needs explicit config + the gateway intake
    import model_tools
    assert "browser_vault" in model_tools._VAULT_NO_PASSWORD_NOTE
    # dlz: owner policy 2026-11-09: one-time codes/callbacks may come from chat, long-lived secrets never.
    from tools.browser_vault_tool import BROWSER_VAULT_SCHEMA
    for text in (model_tools._VAULT_NO_PASSWORD_NOTE, BROWSER_VAULT_SCHEMA["description"],
                 BROWSER_VAULT_SCHEMA["parameters"]["properties"]["code"]["description"]):
        assert "one-time code the user sent" in text and "in chat" in text
        assert "Never ask the user for a code in chat" not in text and "or accept one in chat" not in text
    assert "never accept a password, card data, API key or token" in model_tools._VAULT_NO_PASSWORD_NOTE
    assert "never accept a password, card data, API key or token" in BROWSER_VAULT_SCHEMA["description"]
    desc = tool.CLAUDE_SETUP_TOKEN_SCHEMA["description"]
    assert "authorize_url (send it to Danil in chat" in desc and "back to you in chat" in desc and "call submit" in desc and "never in chat" not in desc
    assert "never ask for or accept the resulting token, a password, API key or card data in chat" in desc
    src = Path(cst.__file__).read_text() + Path(tool.__file__).read_text()
    for forbidden in ("allow_private_urls", "set_code_prompt_callback", "set_unlock_prompt_callback",
                      "redact", "approval"):
        assert forbidden not in src
    from agent.vault_backends.unlock import get_code_prompt_callback
    seen = []
    t = threading.Thread(target=lambda: seen.append(get_code_prompt_callback()))
    t.start(), t.join()
    assert seen == [None]  # headless threads still have no code prompt


def test_private_control_verb_requires_authenticated_peer():
    from gateway.control_socket import GatewayControlServer
    server = GatewayControlServer(home=Path(os.environ.get("HERMES_HOME", "/tmp")))
    cst.register_control_verbs(server)
    line = json.dumps({"protocol": 1, "verb": cst.CONTROL_VERB, "id": 1,
                       "params": {"action": "view", "handoff": "x" * 16, "profile": "/p", "principal": "o"}}).encode()
    assert json.loads(server.handle_request_line(line, None))["ok"] is False
    answer = json.loads(server.handle_request_line(line, "uid:1"))
    assert answer["ok"] is True and answer["result"] == {"ok": False, "error": "not_found"}


# Dashboard intake routes (auth, CSRF, framing, no echo) over the real control-socket envelope ------

@pytest.fixture
def dashboard(env, monkeypatch):
    from fastapi.testclient import TestClient
    from gateway import control_socket
    from hermes_cli import web_server
    make, received, _ = env
    server = control_socket.GatewayControlServer(home=Path(os.environ.get("HERMES_HOME", "/tmp")))
    cst.register_control_verbs(server)
    broker = make()
    cst.install_broker(broker)

    def via_socket(home, verb, *, params=None, timeout=2.0):
        line = json.dumps({"protocol": 1, "verb": verb, "id": 1, "params": params or {}}).encode()
        answer = json.loads(server.handle_request_line(line, "uid:1"))
        return answer.get("result") if answer.get("ok") else None

    monkeypatch.setattr(control_socket, "query_gateway_control", via_socket)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9120")
    hdr = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN, "Sec-Fetch-Site": "same-origin"}
    yield client, hdr, broker, received
    cst.install_broker(cst.Broker())


def test_dashboard_intake_requires_auth_same_origin_json_and_never_echoes(dashboard):
    from hermes_cli.web_routers import claude_setup_token as routes
    client, hdr, broker, received = dashboard
    flow = _start(broker, profile=cst.profile_key(), owner="session-token")
    body = {"handoff": flow.handoff_id, "callback": _callback(flow)}
    for malformed in ({"callback": _callback(flow)}, {**body, "callback": _callback(flow) + "&x=" + "a" * 5000},
                      {**body, "extra": CODE}):
        r = client.post(routes.SUBMIT_PATH, json=malformed, headers=hdr)
        assert r.status_code in (400, 413) and not any(c in r.text for c in CANARIES), r.text
    assert client.post(routes.SUBMIT_PATH, json=body).status_code == 401
    assert client.post(routes.SUBMIT_PATH, json=body, headers={**hdr, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(routes.SUBMIT_PATH, content=json.dumps(body),
                       headers={**hdr, "Content-Type": "text/plain"}).status_code in (415, 422)
    bad = client.post(routes.SUBMIT_PATH, json={**body, "callback": _callback(flow, state="nope")}, headers=hdr)
    assert bad.status_code == 409 and not any(c in bad.text for c in CANARIES)
    assert received() == []
    view = client.post(routes.VIEW_PATH, json={"handoff": flow.handoff_id}, headers=hdr)
    assert view.status_code == 200 and view.json()["authorize_url"].startswith("https://claude.com/")
    ok = client.post(routes.SUBMIT_PATH, json=body, headers=hdr)
    assert ok.status_code == 200 and not any(c in ok.text for c in CANARIES)
    again = client.post(routes.SUBMIT_PATH, json=body, headers=hdr)
    assert again.status_code in (404, 409)
    _settled(flow)
    assert flow.state == cst.STORED and len(received()) == 1
    page = client.get(routes.PAGE_PATH)
    assert page.status_code == 200 and "Claude setup token" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    assert not any(c in page.text for c in CANARIES)
