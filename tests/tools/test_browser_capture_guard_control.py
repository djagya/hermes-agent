"""browser-control's CDP capture guard, runtime side: set before card fill, fail closed. Also the managed
fill's live-ownership fences and item-revision binding (design v2 A6-A8), asserted by call order.

Synthetic leases and a fake control only; no Chrome, no card data.
"""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
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
        documents=(), expires_at=time.monotonic() + 120,
        target_origins=("https://psp.test",),
        prompt=lambda: {"merchant_origin": "https://shop.test", "target_origin": "https://psp.test",
                        "fields": ["cc-number"], "checks": []})

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspected

        def payment_is_current(self, i):
            events.append("current")
            return True

        def commit_payment(self, i, fills, strict=False):
            events.append("commit")
            i.consumed = True
            return {"success": True, "filled_fields": len(fills)}

        def discard_payment(self, i):
            pass

    supervisor = Supervisor()
    inspected.owner = supervisor
    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: supervisor)
    monkeypatch.setattr("tools.browser_vault_tool._managed_supervisor",
                        lambda task_id, admit=False: events.append("admit") or supervisor)
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
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=("https://shop.test",), origin=None,
                           payment_scope="bound", payment_frame_origins=())
    backend = SimpleNamespace(name="local", get_meta=lambda handle: events.append("meta") or meta,
                              resolve_secret=lambda handle: events.append("resolve") or {"card_number": "synthetic"})

    def run(answer, strict=False):
        def fake_set(lease_id, token):
            events.append(("set", lease_id, token))
            return answer

        monkeypatch.setattr(route, "set_capture_guard", fake_set)
        return json.loads(bvp.fill_payment(backend, meta, "task", strict=strict))

    run.backend, run.meta, run.supervisor = backend, meta, supervisor
    return run, events, monkeypatch


def test_card_fill_sets_the_control_guard_before_any_write(fill):
    run, events, _ = fill
    assert run((True, ""))["success"] is True
    # dlz: design v2 A6 order: item re-read, live lease admission, page freshness, then the guard and the local
    # mark BEFORE the card is read, and a second admission (commit fence) before the write.
    assert [e for e in events if e != "admit"] == ["meta", "current", ("set", "lease-shop", "tok-shop"), "local-mark",
                                                   "resolve", "commit"]
    assert events.index("admit") < events.index("current") and events[-2:] == ["admit", "commit"]


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
                       "error": out["error"]} and "local-mark" not in events and "resolve" not in events


def test_strict_fill_ignores_the_rollout_exception_and_needs_a_managed_browser(fill):
    """Unreviewed destinations: a control without the guard route refuses even with the OPTIONAL opt-in,
    and an unmanaged browser is refused before anything is inspected."""
    run, events, monkeypatch = fill
    monkeypatch.setenv(route.CAPTURE_GUARD_OPTIONAL_ENV, "1")
    out = run((False, "control_without_capture_guard"), strict=True)
    assert out["error_type"] == "payment_capture_guard_unavailable"
    assert "resolve" not in events and "commit" not in events
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: False)
    events.clear()
    assert run((True, ""), strict=True)["error_type"] == "payment_destination_requires_managed_browser"
    assert events == []


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


@pytest.mark.parametrize("at, error_type", [
    ("lease_ended", "payment_browser_changed"),        # control: the agent no longer holds the slot
    ("supervisor_replaced", "payment_browser_changed"),  # a re-attach is a different browser connection
    ("route_lease_changed", "payment_browser_changed"),  # takeover + Return to Sera minted a new lease
    ("human_took_desktop", "human_has_control"),
    ("lease_ended_at_commit", "payment_browser_changed"),
])
def test_a_takeover_between_prompt_and_write_never_writes_the_card(fill, at, error_type):
    from tools import browser_vault_tool as vault
    from tools.bot_desktop import lease as desktop

    run, events, monkeypatch = fill
    admits = []

    def admit(task_id, admit=False):
        admits.append(admit)
        events.append("admit")
        if at == "lease_ended" or (at == "lease_ended_at_commit" and len(admits) == 2):
            raise vault._PageUnavailable("lease_unavailable", "lease ended")
        return SimpleNamespace() if at == "supervisor_replaced" else run.supervisor

    routes = iter([{"lease": "lease-shop"}] + [{"lease": "lease-new" if at == "route_lease_changed" else "lease-shop",
                                                "token": "tok-shop", "slot_id": "shopping"}] * 4)
    monkeypatch.setattr(vault, "_managed_supervisor", admit)
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: next(routes))
    if at == "human_took_desktop":
        monkeypatch.setattr(vault, "_bot_desktop_browser_session", lambda task_id: True)
        monkeypatch.setattr(desktop, "assert_agent_may_act",
                            lambda: (_ for _ in ()).throw(desktop.HumanHasControl("human")))
    out = run((True, ""))
    assert out["success"] is False and out["error_type"] == error_type and "commit" not in events
    # Every check asked control (never the cached socket state); a changed route stops before any attach.
    assert all(admits) and bool(admits) is (at != "route_lease_changed")
    if at == "lease_ended_at_commit":  # read inside Hermes, dropped, never written
        assert "resolve" in events and "dropped; no field received it" in out["error"]
    else:  # stopped before the card was read and before the guard was set
        assert "resolve" not in events and "local-mark" not in events


@pytest.mark.parametrize("edit", ["during_consent", "at_read"])
def test_an_edited_card_is_never_written(fill, edit):
    """Design v2 A8: the prompt binds the item's revision; a different revision on the in-context re-read,
    or in the revealed item itself, refuses with payment_item_changed and writes nothing."""
    run, events, _ = fill
    meta = run.meta
    meta.revision = "op:3"
    fresh = SimpleNamespace(**{**vars(meta), "revision": "op:4" if edit == "during_consent" else "op:3"})
    run.backend.get_meta = lambda handle: events.append("meta") or fresh
    run.backend.resolve_payment_secret = lambda handle: (events.append("resolve") or {"card_number": "x"}, "op:4")
    out = run((True, ""))
    assert out["error_type"] == "payment_item_changed" and "commit" not in events
    assert ("resolve" in events) is (edit == "at_read")
