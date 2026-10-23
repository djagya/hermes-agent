"""browser-control's CDP capture guard, runtime side: set before card fill, fail closed.

Synthetic leases and a fake control only; no Chrome, no card data.
"""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tools import browser_control_route as route
from tools import browser_use_cli
from tools import browser_vault_payment as bvp


@pytest.fixture
def fill(monkeypatch):
    """fill_payment on a managed lease with every page/consent step faked; records the order."""
    events = []
    inspected = SimpleNamespace(
        consumed=False, merchant_origin="https://shop.test", frame_origin="https://psp.test",
        target_origins=("https://psp.test",),
        prompt=lambda: {"merchant_origin": "https://shop.test", "target_origin": "https://psp.test",
                        "fields": ["cc-number"], "checks": []})

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspected

        def payment_is_current(self, i):
            return True

        def commit_payment(self, i, fills):
            events.append("commit")
            i.consumed = True
            return {"success": True, "filled_fields": len(fills)}

        def discard_payment(self, i):
            pass

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: True)
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "accept")
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: {
        "lease": "lease-shop", "token": "tok-shop", "slot_id": "shopping", "browser_generation": "g1",
        "wipe_count": "3"})
    monkeypatch.setattr(bvp, "validate_card", lambda secret: secret)
    monkeypatch.setattr(bvp, "effective_frame_origins", lambda origins: origins)
    monkeypatch.setattr(bvp, "_register_card_redaction", lambda secret: None)
    monkeypatch.setattr(bvp, "_document_fills", lambda i, s: [{"index": 0, "token": "cc-number", "document": 0}])
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session",
                        lambda task_id, surface=None: events.append("local-mark"))
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: {"card_number": "synthetic"})
    meta = SimpleNamespace(id="h", label="Card", allowed_origins=("https://shop.test",), origin=None,
                           payment_scope="bound", payment_frame_origins=())

    def run(answer):
        def fake_set(lease_id, token):
            events.append(("set", lease_id, token))
            return answer

        monkeypatch.setattr(route, "set_capture_guard", fake_set)
        return json.loads(bvp.fill_payment(backend, meta, "task"))

    return run, events, monkeypatch


def test_card_fill_sets_the_control_guard_before_any_write(fill):
    run, events, _ = fill
    assert run((True, ""))["success"] is True
    assert events == [("set", "lease-shop", "tok-shop"), "local-mark", "commit"]


@pytest.mark.parametrize("answer, optional, filled", [
    ((False, "control HTTP 500"), "", False),
    ((False, "control unreachable: TimeoutError"), "1", False),  # escape hatch covers 404 only
    ((False, "control_without_capture_guard"), "", False),  # old control: fail closed by default
    ((False, "control_without_capture_guard"), "1", True),  # operator rollout opt-in
])
def test_card_fill_fails_closed_when_the_guard_is_not_set(fill, answer, optional, filled):
    run, events, monkeypatch = fill
    monkeypatch.setenv(route.CAPTURE_GUARD_OPTIONAL_ENV, optional)
    out = run(answer)
    assert ("commit" in events) is filled
    if not filled:
        assert out == {"success": False, "error_type": "payment_capture_guard_unavailable",
                       "error": out["error"]} and "local-mark" not in events


@pytest.fixture
def control(monkeypatch):
    """A fake browser-control answering /v1/capture-guard with a configurable status."""
    seen, reply = [], {"status": 200, "body": {"capture_blocked": True, "slot_id": "shopping"}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, self.headers.get("Authorization"), json.loads(body)))
            data = json.dumps(reply["body"]).encode()
            self.send_response(reply["status"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv(route.MANAGED_CONTROL_ENV, f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv(route.MANAGED_KEY_ENV, "k")
    monkeypatch.delenv(route.MANAGED_KEY_FILE_ENV, raising=False)
    yield seen, reply
    httpd.shutdown()


def test_set_capture_guard_sends_the_lease_and_reads_the_answer(control):
    seen, reply = control
    assert route.set_capture_guard("lease-1", "tok-1") == (True, "")
    assert seen == [("/v1/capture-guard", "Bearer k", {"lease_id": "lease-1", "command_token": "tok-1"})]
    reply.update(status=404, body={"error": "unavailable", "detail": "no such route"})
    assert route.set_capture_guard("lease-1", "tok-1") == (False, "control_without_capture_guard")
    reply.update(status=200, body={"capture_blocked": False})
    assert route.set_capture_guard("lease-1", "tok-1")[0] is False
    assert route.set_capture_guard("", "tok-1") == (False, "no managed lease for this page")


def test_control_capture_refusal_reaches_the_model_as_payment_screenshot_blocked(monkeypatch):
    lease = {"slot_id": "shopping", "browser_generation": "g1", "ownership_epoch": 1,
             "command_token": "tok", "lease_id": "lease"}
    stderr = ("RuntimeError: {'code': -32000, 'message': 'payment_screenshot_blocked: browser-control "
              "refuses capture on this slot after a card fill'}")
    monkeypatch.setattr(route, "is_managed", lambda: True)
    monkeypatch.setattr(browser_use_cli, "_route_backend",
                        lambda env, *a, **k: route._apply_lease(env, lease, "http://control.invalid"))
    monkeypatch.setattr(browser_use_cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(browser_use_cli, "_attach_vault_supervisor", lambda env, task_id: None)
    monkeypatch.setattr(browser_use_cli, "_run_cli_killing_process_group",
                        lambda cmd, code, env, timeout: subprocess.CompletedProcess(cmd, 1, "", stderr))
    # Split literal: the local text guard cannot see it; control's answer still maps.
    out = json.loads(browser_use_cli.browser_exec('cdp("Page.capture" + "Screen" + "shot")', task_id="t"))
    assert out["success"] is False and out["error_type"] == "payment_screenshot_blocked"
