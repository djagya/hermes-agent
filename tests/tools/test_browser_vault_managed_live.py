"""Managed browser_exec navigation + native vault fill through the managed route, on real Chromium.

HERMES_E2E_BROWSER=1 scripts/run_tests.sh -m integration this file.

A stand-in for monolith browser-control (lease/token/status/release/take, ``?tok=`` gated
discovery and WebSocket splice) fronts two isolated Chromiums: one per slot identity. The
browser-use CLI subprocess is emulated in-process exactly as far as the route sees it
(discovery through BU_CDP_URL, then its own new tab), so browser_exec's real route, lease
cache and supervisor attach run. Regression for the JetBrains "Could not determine the
current page origin" report: a persistent slot holding a crashed tab made the supervisor
attach fail, and the vault then acquired a second, identity-less lease on another slot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import select
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("HERMES_E2E_BROWSER") != "1", reason="explicit isolated browser opt-in required"),
]

SENTINEL = "Sentinel-" + secrets.token_hex(6)
LOGIN = ('<form><input name=username type=email autocomplete=username value=user@example.test>'
         '<input name=password type=password autocomplete=current-password><button>Sign in</button></form>')


def _chrome_binary() -> str:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome and mac.exists():
        chrome = str(mac)
    if not chrome:
        pytest.fail("A real Chromium is required for this receipt")
    return chrome


def _https_fixture(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = ("<!doctype html>" + (LOGIN if self.path.startswith("/login") else "<p>no form</p>")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    srv.socket = tls.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _start_chrome(chrome: str, profile: Path):
    log = (profile.parent / f"{profile.name}.log").open("w")
    proc = subprocess.Popen([chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                             "--ignore-certificate-errors", "--no-proxy-server", "about:blank"],
                            stdout=log, stderr=log)
    deadline = time.monotonic() + 20
    while not (profile / "DevToolsActivePort").exists() and time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("Isolated Chromium exited before CDP startup")
        time.sleep(0.1)
    return proc, log, int((profile / "DevToolsActivePort").read_text().splitlines()[0])


def _cdp_session(port: int):
    """Tiny synchronous browser-level CDP client for test setup and readback (never the code under test)."""
    from websockets.sync.client import connect

    with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
        ws = connect(json.load(r)["webSocketDebuggerUrl"], open_timeout=5, max_size=None)
    ids = iter(range(1, 1 << 30))

    def call(method, params=None, sid=None, wait=True):
        msg = {"id": next(ids), "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        ws.send(json.dumps(msg))
        while wait:
            reply = json.loads(ws.recv(10))
            if reply.get("id") == msg["id"]:
                return reply
        return None

    return ws, call


class _Gate:
    """Stand-in for monolith browser-control: one lease per slot, ``?tok=`` on discovery and
    WebSocket upgrade, transports revoked on release/take (the human-control fence)."""

    def __init__(self, slots):
        self.slots = {sid: {"port": port, "identity": ident, "lease": "", "token": "", "task": "", "owner": ""}
                      for sid, (ident, port) in slots.items()}
        # refuse_upgrades: refuse every CDP WebSocket but the harness's (an attach failure only the vault sees)
        self.acquires, self.transports, self.refuse_upgrades = [], {}, False
        self.lock = threading.Lock()
        gate = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/v1/acquire":
                    return self._json(200, gate.acquire(body))
                if self.path == "/v1/release":
                    gate.end(body.get("lease_id", ""), "")
                    return self._json(200, {"ok": True})
                self._json(404, {"error": "unavailable"})

            def do_GET(self):
                url = urlparse(self.path)
                token = (parse_qs(url.query).get("tok") or [""])[0].removesuffix("/json/version")
                if url.path == "/v1/status":
                    lease = (parse_qs(url.query).get("lease") or [""])[0]
                    slot = gate.by(lease=lease)
                    if slot is None:
                        return self._json(409, {"error": "stale"})
                    return self._json(200, {"owner": slot["owner"], "state": slot["owner"]})
                slot = gate.by(token=token)
                if slot is None or slot["owner"] != "agent":
                    return self._json(403, {"error": "stale"})
                if url.path.endswith("/json/version"):
                    with urlopen(f"http://127.0.0.1:{slot['port']}/json/version", timeout=5) as r:
                        payload = json.load(r)
                    path = urlparse(payload["webSocketDebuggerUrl"]).path
                    payload["webSocketDebuggerUrl"] = f"ws://127.0.0.1:{gate.port}{path}?tok={token}"
                    return self._json(200, payload)
                if url.path.startswith("/devtools/") and self.headers.get("Upgrade", "").lower() == "websocket":
                    if gate.refuse_upgrades and not self.headers.get("X-Harness"):
                        return self._json(403, {"error": "scope_denied"})
                    return gate.splice(self, slot, url.path)
                self._json(404, {"error": "unavailable"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def by(self, *, lease="", token=""):
        with self.lock:
            return next((s for s in self.slots.values()
                         if (lease and s["lease"] == lease) or (token and s["token"] == token)), None)

    def acquire(self, body):
        identity = body.get("identity") or "research"
        with self.lock:
            self.acquires.append((body.get("task"), identity))
            sid, slot = next(((k, s) for k, s in self.slots.items() if s["identity"] == identity), (None, None))
            if slot is None:
                return {"error": "unavailable", "detail": f"no slot for {identity}"}
            if slot["owner"] and slot["task"] != body.get("task"):
                return {"error": "busy", "detail": "held"}
            if not slot["owner"]:
                slot.update(lease=f"lease-{secrets.token_hex(4)}", token=f"tok-{secrets.token_hex(8)}",
                            task=body.get("task"), owner="agent")
            return {"slot_id": sid, "browser_generation": "g1", "ownership_epoch": 1,
                    "lease_id": slot["lease"], "command_token": slot["token"]}

    def end(self, lease, new_owner):
        """Release (``new_owner=""``) or human take: kill the token and every open transport."""
        with self.lock:
            slot = next((s for s in self.slots.values() if s["lease"] == lease), None)
            if slot is None:
                return
            socks = self.transports.pop(lease, [])
            slot.update(token="", owner=new_owner, lease=lease if new_owner else "", task=slot["task"] if new_owner else "")
        for sock in socks:
            for s in sock:
                try:
                    s.close()
                except OSError:
                    pass

    def splice(self, handler, slot, path):
        upstream = socket.create_connection(("127.0.0.1", slot["port"]), timeout=5)
        lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{slot['port']}"]
        lines += [f"{k}: {v}" for k, v in handler.headers.items() if k.lower() not in {"host", "origin"}]
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        client = handler.connection
        with self.lock:
            self.transports.setdefault(slot["lease"], []).append((client, upstream))
        handler.close_connection = True
        try:
            while True:
                ready, _, _ = select.select([client, upstream], [], [], 0.5)
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    (upstream if src is client else client).sendall(data)
        except OSError:
            return
        finally:
            upstream.close()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from tools import browser_control_route, browser_use_cli
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    chrome = _chrome_binary()
    fixture = _https_fixture(tmp_path)
    origin = f"https://localhost:{fixture.server_port}"
    procs = [_start_chrome(chrome, tmp_path / name) for name in ("work", "research")]
    gate = _Gate({"work-1": ("work:default", procs[0][2]), "research-1": ("research", procs[1][2])})
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", f"http://127.0.0.1:{gate.port}")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "test-control-key")
    browser_control_route.clear_held_leases()
    argv = []

    def harness(cmd, code, env, timeout):
        """What the browser-use CLI does on the route: discover via BU_CDP_URL, open its own tab."""
        from websockets.sync.client import connect

        argv.append(list(cmd))
        with urlopen(env["BU_CDP_URL"], timeout=10) as r:
            ws = json.load(r)["webSocketDebuggerUrl"]
        with connect(ws, open_timeout=10, max_size=None, additional_headers={"X-Harness": "1"}) as c:
            target = "https://" + re.search(r"TARGET = '([^']+)'", code).group(1)
            c.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": target}}))
            while json.loads(c.recv(10)).get("id") != 1:
                pass
        time.sleep(1.0)
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(browser_use_cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(browser_use_cli, "_run_cli_killing_process_group", harness)
    try:
        yield {"origin": origin, "gate": gate, "work": procs[0][2], "research": procs[1][2], "argv": argv}
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        browser_control_route.clear_held_leases()
        browser_use_cli._VAULT_ROUTES.clear()
        gate.close()
        for proc, log, _ in procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
        fixture.shutdown()
        fixture.server_close()


def _vault_backend(monkeypatch, origin):
    from agent.vault_store import VaultItemMeta

    meta = VaultItemMeta(id="op:synthetic", kind="login", label="Synthetic", origin=origin,
                         created_at="2026-10-08T00:00:00Z", identifier_type="email",
                         identifier="user@example.test", allowed_origins=(origin,))

    class Backend:
        name, display_name, needs_unlock = "onepassword", "1Password", False

        def is_unlocked(self):
            return True

        def get_meta(self, handle):
            return meta if handle == meta.id else None

        def resolve_password(self, handle):
            return SENTINEL

    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda handle: Backend())


def _seed_persistent_slot(port, origin):
    """A long-lived identity slot: a renderer-crashed tab first, a stale background login tab of the same site."""
    ws, call = _cdp_session(port)
    with ws:
        blank = [t["targetId"] for t in call("Target.getTargets")["result"]["targetInfos"] if t["type"] == "page"]
        crashed = call("Target.createTarget", {"url": origin + "/other", "background": True})["result"]["targetId"]
        time.sleep(0.8)
        sid = call("Target.attachToTarget", {"targetId": crashed, "flatten": True})["result"]["sessionId"]
        call("Page.crash", sid=sid, wait=False)
        call("Target.createTarget", {"url": origin + "/login?stale=1", "background": True})
        time.sleep(0.8)
        for tid in blank:
            call("Target.closeTarget", {"targetId": tid})


def _password_lengths(port):
    """Password value length per live http(s) tab, read straight from the browser (crashed tabs skipped)."""
    out = {}
    ws, call = _cdp_session(port)
    with ws:
        for t in call("Target.getTargets")["result"]["targetInfos"]:
            if t["type"] != "page" or not t["url"].startswith("https://") or "/other" in t["url"]:
                continue
            sid = call("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True})["result"]["sessionId"]
            got = call("Runtime.evaluate", {"returnByValue": True, "expression":
                       "(document.querySelector('input[type=password]')||{value:''}).value.length"}, sid=sid)
            out[t["url"]] = got["result"]["result"].get("value")
    return out


def _logged(caplog) -> str:
    """What reaches agent.log (INFO and up); third-party DEBUG wire traces are below it."""
    return "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)


def _exec(task, url, **kw):
    from tools.browser_use_cli import browser_exec

    # Scheme-less, so browser_exec's URL-literal safety check (which refuses localhost) is not what is tested.
    code = f"# open the login page\nTARGET = '{url.split('://', 1)[1]}'"
    return json.loads(browser_exec(code=code, task_id=task, mode="research", identity="work:default", **kw))


def _fill(task):
    from tools.browser_vault_tool import _handle_vault_fill

    raw = _handle_vault_fill({"handle": "op:synthetic"}, task_id=task)
    return raw, json.loads(raw)


@pytest.mark.parametrize("session", ["jetbrains-cancel", ""])
def test_exec_and_vault_fill_share_the_managed_browser(managed, monkeypatch, caplog, session):
    """The vault writes into the tab browser_exec opened on the leased identity browser, despite a crashed
    and a stale same-site tab in that persistent slot, without a second lease and without the secret
    reaching tool results, logs or argv."""
    origin, gate = managed["origin"], managed["gate"]
    _vault_backend(monkeypatch, origin)
    _seed_persistent_slot(managed["work"], origin)
    caplog.set_level("DEBUG")

    assert _exec("task-a", origin + "/login", session=session)["success"] is True
    raw, out = _fill("task-a")

    assert out["success"] is True, out
    assert out["origin"] == origin
    filled = _password_lengths(managed["work"])
    assert filled[origin + "/login"] == len(SENTINEL)
    assert filled[origin + "/login?stale=1"] == 0
    assert gate.acquires == [("task-a", "work:default")]  # no identity-less second lease on another slot
    assert not any("/login" in u for u in _password_lengths(managed["research"]))
    assert SENTINEL not in raw
    assert SENTINEL not in caplog.text
    assert not any(SENTINEL in " ".join(a) for a in managed["argv"])
    token = gate.slots["work-1"]["token"]
    assert token and token not in _logged(caplog)


def test_vault_refusals_are_typed_and_never_reroute(managed, monkeypatch, caplog):
    """Each way the vault cannot reach the exec's page names its cause, acquires nothing and writes nothing."""
    origin, gate = managed["origin"], managed["gate"]
    _vault_backend(monkeypatch, origin)
    caplog.set_level("DEBUG")

    # A task that never ran browser_exec has no page; it must not borrow or acquire one.
    assert _fill("task-b")[1]["error_type"] == "no_browser_session"

    # The exec's tab is on another site than the item's: refused by name, not as a missing page.
    other = origin.replace("localhost", "127.0.0.1")
    assert _exec("task-a", other + "/login")["success"] is True
    out = _fill("task-a")[1]
    assert out["error_type"] == "origin_mismatch" and other in out["error"]

    # Human takeover and release both kill the lease; the vault refuses before any write.
    assert _exec("task-a", origin + "/login")["success"] is True
    lease = gate.slots["work-1"]["lease"]
    gate.end(lease, "human")
    assert _fill("task-a")[1]["error_type"] == "lease_unavailable"
    gate.end(lease, "")
    assert _fill("task-a")[1]["error_type"] == "lease_unavailable"

    # A gate that refuses the vault's WebSocket: exec still runs, the vault reports why, token redacted.
    from tools.browser_control_route import clear_held_leases
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    SUPERVISOR_REGISTRY.stop_all()
    clear_held_leases()
    gate.refuse_upgrades = True
    assert _exec("task-c", origin + "/login")["success"] is True
    out = _fill("task-c")[1]
    assert out["error_type"] == "supervisor_unavailable"
    token = gate.slots["work-1"]["token"]
    assert token and token not in json.dumps(out) and token not in _logged(caplog)

    assert all(identity == "work:default" for _, identity in gate.acquires)
    assert all(v == 0 for v in _password_lengths(managed["work"]).values())
    assert SENTINEL not in caplog.text
