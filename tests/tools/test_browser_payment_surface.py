"""Payment slot lockdown, runtime side: claim of the payment tab before resolve, the one lock check on the
native socket (browser_cdp frame route, dialogs, vault page ops), withheld in-flight replies, native
payment_controls / payment_click. Fake supervisor and fake CDP only; synthetic data."""

import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from tools import browser_cdp_tool as cdp_tool
from tools import browser_payment_surface as surface
from tools import browser_use_cli
from tools import browser_vault_payment as bvp
from tests.waiting import wait_until

TREE = (("PAGE", "LP", "https://shop.test/", "https://shop.test", ""),
        ("CARD", "L1", "https://psp.test/f", "https://psp.test", "PAGE"))
ROWS = [
    {"i": 0, "role": "button", "text": "Pay with card 4242 4242 4242 4242", "disabled": False},
    {"i": 1, "role": "link", "text": "Terms", "disabled": False},
    {"i": 2, "role": "checkbox", "text": "Save this card", "disabled": True},
]


class FakeSup:
    """The supervisor surface the module touches, on a real loop thread; CDP answered by ``on``."""

    def __init__(self, on=None):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._ws, self._state_lock = object(), threading.Lock()
        self._page_session_id, self._active = "S-page", True
        self.calls, self.status = [], {"locked": False, "native": False, "tabs": [], "unverified": False}
        self.hit, self.at_point = True, 77
        self.on = on or self.default
        self.delays = {}  # method -> seconds before its reply (a slow Chrome)

    def default(self, method, params, session):
        if method == "Hermes.claimPaymentSurface":
            self.status = {"locked": True, "native": True, "tabs": list(params["tabs"]), "unverified": False}
            return {"claimed": True, "locked": True, "tabs": list(params["tabs"])}
        if method == "Hermes.paymentSurfaceStatus":
            if isinstance(self.status, Exception):
                raise self.status
            return self.status
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "PAGE", "type": "page"}, {"targetId": "CARD", "type": "iframe"}]}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 5}
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "arr"}}
        if method == "Runtime.callFunctionOn":
            fn = params["functionDeclaration"]
            if fn == surface._DESCRIBE:
                return {"result": {"value": ROWS}}
            if fn == surface._HIT:
                return {"result": {"value": self.hit}}
            return {"result": {"objectId": "el"}}
        if method == "DOM.getContentQuads":
            return {"quads": [[10, 10, 30, 10, 30, 30, 10, 30]]}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 77}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [100, 200, 400, 200, 400, 300, 100, 300]}}
        if method == "DOM.getNodeForLocation":
            return {"backendNodeId": self.at_point}
        return {}

    async def _cdp(self, method, params=None, *, session_id=None, timeout=10.0):
        self.calls.append((method, params or {}, session_id))
        if method in self.delays:
            await asyncio.sleep(self.delays[method])
        return {"id": len(self.calls), "result": self.on(method, params or {}, session_id)}

    async def _payment_tree(self, sid, child_sessions):
        child_sessions["CARD"] = "S-card"
        return TREE

    async def _release_payment_object(self, sid, obj):
        pass

    async def _release_payment_sessions(self, sessions):
        pass

    def sent(self, method):
        return [c for c in self.calls if c[0] == method]


def _inspected(page="PAGE"):
    docs = (SimpleNamespace(frame_id="CARD", frame_session="S-card", origin="https://psp.test"),
            SimpleNamespace(frame_id="NAMEF", frame_session="S-page", origin="https://shop.test"))
    return SimpleNamespace(documents=docs, page_session="S-page", page_id=page, merchant_origin="https://shop.test",
                           tree=TREE, child_sessions={"CARD": "S-card", "INNER": "S-card"})


UNLOCKED = {"locked": False, "native": False, "tabs": [], "unverified": False}


@pytest.fixture(autouse=True)
def _unmanaged(monkeypatch):
    # unknown status means locked only on a managed browser; these fakes run as a browser without control
    monkeypatch.setattr(surface, "_managed", lambda: False)


def _claimed(sup=None):
    sup = sup or FakeSup()
    assert surface.claim_payment_surface(sup, _inspected()) is None
    return sup


def test_claim_names_the_payment_tab_only():
    sup = _claimed()
    (_, params, session), = sup.sent("Hermes.claimPaymentSurface")
    assert params == {"tabs": ["PAGE"]} and session is None
    assert surface.slot_locked(sup) is True


@pytest.mark.parametrize("answer, kept", [
    # control's own refusal: it changed nothing, so the local lock is not kept
    (RuntimeError("CDP error on id=3: {'message': 'payment_surface_unavailable: held by another connection'}"),
     False),
    # uncertain outcomes keep the lock here until control's status proves the slot unlocked
    ({"claimed": True, "locked": True, "tabs": ["OTHER"]}, True),  # does not cover the payment tab
    ({"claimed": True, "tabs": ["PAGE"]}, True),
    ({"claimed": True, "locked": True, "tabs": ["PAGE"], "frames": []}, True),
    ({"claimed": False}, True),
    (RuntimeError("boom"), True),
])
def test_unconfirmed_claim_refuses_with_a_fixed_type(answer, kept):
    sup = FakeSup()

    def on(method, params, session):
        if method == "Hermes.claimPaymentSurface":
            if isinstance(answer, Exception):
                raise answer
            return answer
        return sup.default(method, params, session)

    sup.on = on
    out = surface.claim_payment_surface(sup, _inspected())
    assert out["error_type"] == "payment_surface_unavailable" and "another connection" not in out["error"]
    sup.status = RuntimeError("status unreachable")  # only the local lock decides
    assert surface.slot_locked(sup) is kept
    sup.status = dict(UNLOCKED)  # control proves the slot unlocked: the local lock lifts
    assert surface.slot_locked(sup) is False


@pytest.mark.parametrize("failure", [
    RuntimeError("CDP error on id=9: {'message': 'payment_surface_unavailable: could not verify the tabs'}"),
    RuntimeError("socket reset"),
])
def test_a_failed_extension_never_lifts_an_earlier_fill(failure):
    sup = _claimed()

    def on(method, params, session):
        if method in {"Hermes.claimPaymentSurface", "Hermes.paymentSurfaceStatus"}:
            raise failure
        return sup.default(method, params, session)

    sup.on = on
    assert surface.claim_payment_surface(sup, _inspected("PAGE2"))["error_type"] == "payment_surface_unavailable"
    assert surface.slot_locked(sup) is True


@pytest.mark.parametrize("status, managed, locked", [
    ({"locked": True, "native": True, "tabs": ["PAGE"], "unverified": False}, False, True),
    ({"locked": True, "native": False, "tabs": [], "unverified": True}, False, True),  # human clear only
    (UNLOCKED, True, False),
    # malformed or unknown answers: locked on a managed browser, never "unlocked"
    ({"locked": False, "native": False, "tabs": ["PAGE"], "unverified": False}, True, True),
    ({"locked": False, "native": True, "tabs": [], "unverified": False}, True, True),
    ({"locked": "false", "native": False, "tabs": [], "unverified": False}, True, True),
    ({"locked": False, "tabs": [], "unverified": False}, True, True),
    ({**UNLOCKED, "extra": 1}, True, True),
    ({"protected": False, "frames": [], "targets": []}, True, True),  # the pre-lockdown shape
    (RuntimeError("wasn't found"), True, True),
    (RuntimeError("wasn't found"), False, False),  # a browser without control holds no lock
])
def test_lock_state_is_read_strictly(monkeypatch, status, managed, locked):
    sup = FakeSup()
    sup.status = status
    monkeypatch.setattr(surface, "_managed", lambda: managed)
    assert surface.slot_locked(sup) is locked
    assert surface.parse_lock_status(None) == ("unknown", frozenset())


def test_the_lock_is_set_before_the_claim_is_sent():
    # A relayed call checked while the claim is on the wire is refused, whatever a status read says then.
    sup = FakeSup()
    at_claim = {}

    def on(method, params, session):
        if method == "Hermes.claimPaymentSurface":
            with surface._LOCK:
                at_claim.update(held=sup in surface._HELD, claiming=sup in surface._CLAIMING)
        return sup.default(method, params, session)

    sup.on = on
    before = surface.epoch()
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert at_claim == {"held": True, "claiming": True} and surface.epoch() == before + 1
    assert sup not in surface._CLAIMING


@pytest.fixture
def fill(monkeypatch):
    """fill_payment on a managed lease; every step but the claim faked; records the order.
    Mirrors tests/tools/test_browser_capture_guard_control.py's fixture (approval card facts, admissions)."""
    events = []
    inspected = SimpleNamespace(
        consumed=False, merchant_origin="https://shop.test", frame_origin="https://psp.test",
        operation_id="f" * 32, documents=(SimpleNamespace(origin="https://psp.test",
                                                          controls=(SimpleNamespace(token="cc-number"),)),),
        expires_at=time.monotonic() + 120, target_origins=("https://psp.test",),
        prompt=lambda: {"merchant_origin": "https://shop.test", "target_origin": "https://psp.test",
                        "fields": ["cc-number"], "checks": []})
    sup = SimpleNamespace(inspect_payment=lambda *a, **k: inspected, payment_is_current=lambda i: True,
                          commit_payment=lambda i, f, strict=False: events.append("commit") or {"success": True},
                          discard_payment=lambda i: None)
    inspected.owner = sup
    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: sup)
    monkeypatch.setattr("tools.browser_vault_tool._managed_supervisor", lambda task_id, admit=False: sup)
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: True)
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "accept")
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: {"lease": "l", "token": "t"})
    monkeypatch.setattr(bvp, "validate_card", lambda secret: secret)
    monkeypatch.setattr(bvp, "effective_frame_origins", lambda origins: origins)
    monkeypatch.setattr(bvp, "_register_card_redaction", lambda secret: None)
    monkeypatch.setattr(bvp, "_document_fills", lambda i, s: [{"index": 0, "token": "cc-number", "document": 0}])
    monkeypatch.setattr("tools.browser_control_route.set_capture_guard", lambda l, t: events.append("guard") or (True, ""))
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda *a, **k: None)
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=("https://shop.test",), origin=None,
                           payment_scope="bound", payment_frame_origins=())
    backend = SimpleNamespace(name="local", get_meta=lambda h: meta,
                              resolve_secret=lambda h: events.append("resolve") or {"n": "synthetic"})
    return lambda: json.loads(bvp.fill_payment(backend, meta, "task")), events, monkeypatch


def test_fill_refuses_without_a_confirmed_claim_and_resolves_nothing(fill):
    run, events, monkeypatch = fill
    # The real hook against a supervisor without a loop: the claim cannot be confirmed. The capture guard
    # (set-only, harmless) precedes the claim; no card byte is read.
    out = run()
    assert out["error_type"] == "payment_surface_unavailable" and "resolve" not in events
    events.clear()
    monkeypatch.setattr("tools.browser_payment_surface.claim_payment_surface",
                        lambda sup, i: events.append("claim"))
    assert run()["success"] is True
    assert events == ["guard", "claim", "resolve", "commit"]


def test_consent_expiring_during_the_claim_resolves_nothing(fill):
    # Codex A3 r1 #7: a slow claim verification can outlast the approval window; expiry is checked again
    # right after the claim, before anything is resolved.
    run, events, monkeypatch = fill

    def slow_claim(sup, inspected):
        events.append("claim")
        inspected.expires_at = time.monotonic() - 1

    monkeypatch.setattr("tools.browser_payment_surface.claim_payment_surface", slow_claim)
    out = run()
    assert out["error_type"] == "payment_consent_expired" and "resolve" not in events


def test_consent_expiring_during_the_final_readmission_resolves_nothing(fill):
    # Codex A3 r2 #2: the re-admission after the claim waits on control's status; expiry is checked again
    # after it, immediately before the card is read.
    run, events, monkeypatch = fill
    monkeypatch.setattr("tools.browser_payment_surface.claim_payment_surface",
                        lambda sup, i: events.append("claim"))
    real = bvp._ownership_refusal

    def slow_readmission(vault, task_id, supervisor, inspected, lease):
        if "claim" in events:
            inspected.expires_at = time.monotonic() - 1
        return real(vault, task_id, supervisor, inspected, lease)

    monkeypatch.setattr(bvp, "_ownership_refusal", slow_readmission)
    out = run()
    assert out["error_type"] == "payment_consent_expired" and "resolve" not in events


@pytest.fixture
def routed(monkeypatch):
    sup = FakeSup()
    frames = [{"frame_id": "PAGE"}, {"frame_id": "CARD", "session_id": "S-card"},
              {"frame_id": "ADS", "session_id": "S-ads"}]
    sup.snapshot = lambda: SimpleNamespace(frame_tree={"top": frames[0], "children": frames[1:]})
    sup._frames = {f["frame_id"]: SimpleNamespace(cdp_session_id=f.get("session_id")) for f in frames}
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup)

    def call(method, frame_id="ADS", params=None):
        before = len(sup.calls)
        out = json.loads(cdp_tool._browser_cdp_via_supervisor("t", frame_id, method, params or {}, 5))
        forwarded = [c for c in sup.calls[before:] if c[0] == method]
        return out, bool(forwarded)

    return sup, call


@pytest.mark.parametrize("method, params", [
    ("Runtime.evaluate", {"expression": "document.querySelector('input').value"}),
    ("DOM.getDocument", {}),
    ("DOM.querySelector", {"nodeId": 1, "selector": "input"}),
    ("Page.captureScreenshot", {}),
    ("Target.attachToTarget", {"targetId": "PAGE", "flatten": True}),
    ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}),
    ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "c", "modifiers": 2}),
    ("Network.getResponseBody", {"requestId": "1"}),
    ("Page.navigate", {"url": "https://shop.test/"}),
    ("Target.getTargets", {}),  # even an allowed command: on this route it always carries a session
])
def test_browser_cdp_via_supervisor_refuses_everything_while_locked(routed, method, params):
    sup, call = routed
    assert surface.claim_payment_surface(sup, _inspected()) is None
    out, forwarded = call(method, "ADS", params)
    assert out["error_type"] == "payment_surface_locked" and not forwarded


def test_locked_commands_without_a_session_pass_the_lock_check():
    sup = _claimed()
    for method in surface.LOCKED_COMMANDS:
        assert surface.locked_refusal(sup, method, None) is None
        assert surface.locked_refusal(sup, method, "S-page")["error_type"] == "payment_surface_locked"
    assert surface.locked_refusal(sup)["error_type"] == "payment_surface_locked"


def test_browser_cdp_via_supervisor_is_unaffected_when_unlocked(routed):
    sup, call = routed
    assert call("Runtime.evaluate")[1] and call("DOM.getDocument", "CARD")[1]
    # Model code can never claim as the runtime, locked or not.
    out, forwarded = call("Hermes.claimPaymentSurface", "ADS", {"tabs": ["PAGE"]})
    assert out["error_type"] == "payment_surface_locked" and not forwarded
    # The slot was released (the payment tab destroyed): a claim from earlier no longer blocks.
    assert surface.claim_payment_surface(sup, _inspected()) is None
    sup.status = dict(UNLOCKED)
    assert call("Runtime.evaluate")[1]


def test_a_reply_in_flight_when_a_claim_starts_is_withheld(routed):
    # An awaitPromise evaluation started before the fill could resolve with what the card fields receive.
    sup, call = routed
    sup.delays = {"Runtime.evaluate": 0.6}
    got = {}
    thread = threading.Thread(target=lambda: got.update(out=call("Runtime.evaluate", "ADS", {
        "expression": "new Promise(r => setTimeout(() => r(document.querySelector('input').value), 500))",
        "awaitPromise": True})))
    thread.start()
    wait_until(lambda: sup.sent("Runtime.evaluate"), timeout=5)  # the evaluation is on the wire
    assert surface.claim_payment_surface(sup, _inspected()) is None
    thread.join(5)
    out, forwarded = got["out"]
    assert forwarded and out["error_type"] == "payment_surface_locked" and "withheld" in out["error"]
    assert "result" not in out


def test_dialogs_and_vault_page_ops_refused_while_locked(monkeypatch):
    from tools import browser_dialog_tool, browser_exec_dialogs
    from tools import browser_vault_tool as vault

    sup = _claimed()
    sup.respond_to_dialog = lambda *a, **k: pytest.fail("a dialog answer reached the locked slot")
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup)
    assert json.loads(browser_dialog_tool.browser_dialog("accept"))["error_type"] == "payment_surface_locked"
    assert browser_exec_dialogs.respond("t", action="accept")["error_type"] == "payment_surface_locked"
    assert json.loads(vault._payment_lock_refusal("t"))["error_type"] == "payment_surface_locked"
    dialog = SimpleNamespace(to_dict=lambda: {"id": "d1", "type": "confirm", "message": "Order 4242 total"},
                             url="https://shop.test/x", top_url="https://shop.test/")
    sup.snapshot = lambda: SimpleNamespace(pending_dialogs=[dialog], recent_dialogs=[])
    found = browser_exec_dialogs.collect("t", 0)
    assert found["pending"] == [{"id": "d1", "type": "confirm", "withheld": "payment_surface_locked"}]
    sup.status = dict(UNLOCKED)
    assert vault._payment_lock_refusal("t") is None


def test_payment_controls_mask_digit_runs_and_refs_are_stable():
    sup = _claimed()
    out = surface.payment_controls(sup)
    assert out["success"] is True and out["documents"] == 1
    assert [c["role"] for c in out["controls"]] == ["button", "link", "checkbox"]
    assert out["controls"][0]["text"] == "Pay with card •••• •••• •••• ••••"
    assert not any(re.search(r"\d{4}", c["text"]) for c in out["controls"])
    assert all(set(c) == {"ref", "role", "text", "disabled"} for c in out["controls"])
    assert surface.payment_controls(sup)["controls"] == out["controls"]
    # The lister ran in an isolated world of the protected frame, never the page's own world.
    (_, world, session), = sup.sent("Page.createIsolatedWorld")[:1]
    assert world["frameId"] == "CARD" and session == "S-card"


def test_payment_click_is_one_trusted_click_at_the_hit_tested_centre():
    sup = _claimed()
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    assert surface.payment_click(sup, ref) == {"success": True, "clicked": ref, "role": "button",
                                               "text": "Pay with card •••• •••• •••• ••••"}
    clicks = sup.sent("Input.dispatchMouseEvent")
    assert [c[1]["type"] for c in clicks] == ["mouseMoved", "mousePressed", "mouseReleased"]
    # Quad centre (20, 20) in the card frame plus its iframe's content box (100, 200) on the page.
    assert {(c[1]["x"], c[1]["y"], c[2]) for c in clicks} == {(120, 220, "S-page")}


def test_payment_click_reports_uncertain_after_dispatch_starts():
    sup = _claimed()
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    seen = {"pressed": False}
    base = sup.on

    def on(method, params, session):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
            seen["pressed"] = True
            raise TimeoutError("commit budget")
        return base(method, params, session)

    sup.on = on
    out = surface.payment_click(sup, ref)
    assert seen["pressed"] and out["error_type"] == "payment_click_uncertain"
    assert "Do NOT click again" in out["error"]


@pytest.mark.parametrize("timeout, margin, delay", [
    (0.5, 0.2, 0.9),  # the caller gives up first: the cancelled action must never press later
    (1.0, 0.7, 0.5),  # the press deadline passes while the caller still waits: no press either
])
def test_a_late_click_never_presses_after_reporting_nothing_clicked(monkeypatch, timeout, margin, delay):
    # Codex A3 r1 #5: slow (successful) CDP replies push the geometry past the action's budget before the
    # press. The action reports nothing was clicked, so mousePressed must never be sent, now or later.
    from tools import browser_supervisor

    sup = _claimed()
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    real = browser_supervisor._schedule
    monkeypatch.setattr(browser_supervisor, "_schedule", lambda c, l, timeout: real(c, l, timeout=min(timeout, 0.5)))
    monkeypatch.setattr(surface, "_ACTION_TIMEOUT_S", timeout, raising=False)
    monkeypatch.setattr(surface, "_PRESS_MARGIN_S", margin, raising=False)
    sup.delays = {"DOM.getContentQuads": delay}
    out = surface.payment_click(sup, ref)
    assert out["error_type"] == "payment_surface_unavailable" and "nothing was clicked" in out["error"]
    time.sleep(delay + 0.3)
    assert not [c for c in sup.sent("Input.dispatchMouseEvent") if c[1]["type"] == "mousePressed"]


@pytest.mark.parametrize("change, error_type", [
    ("ref", "payment_control_unknown"),       # a card field never gets a ref: none can be clicked
    ("disabled", "payment_control_disabled"),
    ("covered", "payment_control_covered"),
    ("overlay", "payment_control_covered"),   # another page element sits over the card iframe
    ("lifted", "payment_surface_none"),
])
def test_payment_click_refuses(change, error_type):
    sup = _claimed()
    controls = surface.payment_controls(sup)["controls"]
    ref = controls[2]["ref"] if change == "disabled" else controls[0]["ref"]
    if change == "ref":
        ref = surface._ref("CARD", 3, "button", "card number")
    sup.hit = change != "covered"
    sup.at_point = 99 if change == "overlay" else 77
    if change == "lifted":  # the payment was handed over (payment_done): no native actions remain
        from tools import browser_payment_done

        browser_payment_done._forget(sup, "PAGE")
    assert surface.payment_click(sup, ref)["error_type"] == error_type
    assert not sup.sent("Input.dispatchMouseEvent")


_LISTER_HARNESS = r"""
const E = (tag, o) => Object.assign({tagName: tag, type: tag === 'BUTTON' ? 'submit' : '', disabled: false, value: '', innerText: '', title: '',
  attrs: {}, edit: false, holdsEdit: false, getAttribute(n) { return this.attrs[n] ?? null; },
  matches() { return this.edit; }, querySelector() { return this.holdsEdit ? {} : null; },
  getBoundingClientRect: () => ({width: 50, height: 20})}, o);
const els = [
  E('BUTTON', {innerText: 'Pay  4242 4242'}),
  E('BUTTON', {innerText: 'Cancel', value: 'opaque-form-value'}),
  E('INPUT', {type: 'text', edit: true, value: '4111111111111111'}),
  E('DIV', {attrs: {role: 'button'}, holdsEdit: true, innerText: 'card number 4111'}),
  E('LABEL', {control: {matches: () => true}, innerText: 'Card number'}),
  E('INPUT', {type: 'checkbox', attrs: {'aria-label': 'Save card'}}),
  E('INPUT', {type: 'submit', value: 'Confirm'}),
  E('A', {innerText: 'Terms', getBoundingClientRect: () => ({width: 0, height: 0})}),
];
const document = {querySelectorAll: () => els};
const getComputedStyle = () => ({visibility: 'visible', display: 'block'});
const list = LISTER;
const rows = (DESCRIBE).call(list);
process.stdout.write(JSON.stringify(rows));
"""


def test_lister_never_lists_inputs_selects_or_controls_holding_them():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic lister")
    harness = _LISTER_HARNESS.replace("LISTER", surface._LISTER).replace("DESCRIBE", surface._DESCRIBE)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    rows = json.loads(run.stdout)
    assert [(r["role"], r["text"]) for r in rows] == [
        ("button", "Pay 4242 4242"), ("button", "Cancel"), ("checkbox", "Save card"), ("button", "Confirm")]
    # Python masks the digit runs before anything reaches the model.
    assert surface._mask(rows[0]["text"]) == "Pay •••• ••••"
