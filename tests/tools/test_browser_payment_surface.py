"""Protected payment surface, runtime side (design v2 A9): claim before resolve, browser_cdp gate,
native payment_controls / payment_click. Fake supervisor and fake CDP only; synthetic data."""

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
from tests.waiting import async_wait_until, wait_until

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
        self.calls, self.status = [], {"protected": True, "native": True, "frames": ["CARD"],
                                        "targets": ["CARD"]}
        self.hit, self.at_point = True, 77
        self.on = on or self.default
        self.delays = {}  # method -> seconds before its reply (a slow Chrome)

    def default(self, method, params, session):
        if method == "Hermes.claimPaymentSurface":
            return {"claimed": True, "frames": params["frames"], "targets": params["targets"]}
        if method == "Hermes.paymentSurfaceStatus":
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


def _inspected():
    docs = (SimpleNamespace(frame_id="CARD", frame_session="S-card"),
            SimpleNamespace(frame_id="NAMEF", frame_session="S-page"))
    return SimpleNamespace(documents=docs, page_session="S-page", page_id="PAGE",
                           child_sessions={"CARD": "S-card", "INNER": "S-card"})


def _claimed(sup=None):
    sup = sup or FakeSup()
    assert surface.claim_payment_surface(sup, _inspected()) is None
    return sup


def test_claim_names_each_card_document_and_the_target_holding_it():
    sup = _claimed()
    (_, params, session), = sup.sent("Hermes.claimPaymentSurface")
    assert params == {"frames": ["CARD", "NAMEF"], "targets": ["CARD", "PAGE"]} and session is None


@pytest.mark.parametrize("answer, kept", [
    # control's own refusal: it changed nothing, so nothing stays registered here
    (RuntimeError("CDP error on id=3: {'message': 'payment_surface_unavailable: held by another connection'}"),
     False),
    # uncertain outcomes keep the requested scope until a status refresh narrows or lifts it
    ({"claimed": True, "frames": ["CARD"], "targets": ["CARD"]}, True),  # does not cover NAMEF/PAGE
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
    with surface._LOCK:
        found = surface._SURFACES.get(sup)
    assert (found.frames if found else frozenset()) == ({"CARD", "NAMEF"} if kept else frozenset())


@pytest.mark.parametrize("failure", [
    RuntimeError("CDP error on id=9: {'message': 'payment_surface_unavailable: could not verify the frames'}"),
    RuntimeError("socket reset"),
])
def test_a_failed_extension_never_lifts_an_earlier_fill(failure):
    # Codex A3 r1 #3: CARD is filled and protected; a later claim for another document fails. Control still
    # treats this socket as native for CARD, so the gate here must keep refusing reads of CARD.
    sup = _claimed()
    later = SimpleNamespace(documents=(SimpleNamespace(frame_id="CARD2", frame_session="S-page"),),
                            page_session="S-page", page_id="PAGE", child_sessions={})

    def on(method, params, session):
        if method in {"Hermes.claimPaymentSurface", "Hermes.paymentSurfaceStatus"}:
            raise failure  # status unreachable too: only the cache decides
        return sup.default(method, params, session)

    sup.on = on
    assert surface.claim_payment_surface(sup, later)["error_type"] == "payment_surface_unavailable"
    frames = surface.protected_frames(sup)
    assert {"CARD", "NAMEF"} <= frames
    assert ("CARD2" in frames) is not surface._control_refused(failure), "uncertain: keep the wider scope"


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
    sup = _claimed()
    frames = [{"frame_id": "PAGE"}, {"frame_id": "CARD", "session_id": "S-card"},
              {"frame_id": "ADS", "session_id": "S-ads"},
              # a same-process frame inside the card OOPIF: it shares the card's session
              {"frame_id": "INNER", "session_id": "S-card"}]
    sup.snapshot = lambda: SimpleNamespace(frame_tree={"top": frames[0], "children": frames[1:]})
    sup._frames = {f["frame_id"]: SimpleNamespace(cdp_session_id=f.get("session_id")) for f in frames}
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup)

    def call(method, frame_id="CARD", params=None):
        before = len(sup.calls)
        out = json.loads(cdp_tool._browser_cdp_via_supervisor("t", frame_id, method, params or {}, 5))
        forwarded = [c for c in sup.calls[before:] if c[0] == method]
        return out, bool(forwarded)

    return sup, call


@pytest.mark.parametrize("method, frame_id, params", [
    ("Runtime.evaluate", "CARD", {"expression": "document.querySelector('input').value"}),
    ("DOM.getDocument", "CARD", {}),
    ("Accessibility.getFullAXTree", "CARD", {}),
    ("Runtime.evaluate", "INNER", {"expression": "parent.document.querySelector('input').value"}),
    ("Network.getResponseBody", "ADS", {"requestId": "1"}),
    ("Page.createIsolatedWorld", "ADS", {"frameId": "CARD"}),
    ("Input.dispatchKeyEvent", "ADS", {"type": "keyDown", "key": "c", "modifiers": 2}),
    ("Page.navigate", "CARD", {"url": "javascript:alert(1)"}),
    # Codex A3 r1 #2: Page.* on a protected document is an allowlist, as on control
    ("Page.addScriptToEvaluateOnNewDocument", "CARD",
     {"source": "history.replaceState(0, '', '#' + btoa(document.querySelector('input').value))",
      "runImmediately": True}),
    ("Page.setDocumentContent", "INNER", {"frameId": "INNER", "html": "<b>"}),
    ("Page.setBypassCSP", "CARD", {"enabled": True}),
    # and resource-content reads are refused slot-wide
    ("Page.getResourceContent", "ADS", {"frameId": "ADS", "url": "https://ads.test/x.js"}),
    ("Page.searchInResource", "ADS", {"frameId": "ADS", "url": "https://ads.test/x.js", "query": "4"}),
])
def test_browser_cdp_via_supervisor_refuses_protected_frame_reads(routed, method, frame_id, params):
    _, call = routed
    out, forwarded = call(method, frame_id, params)
    assert out["error_type"] == "payment_surface_protected" and not forwarded


def test_browser_cdp_via_supervisor_keeps_other_frames_and_lifts_with_control(routed):
    sup, call = routed
    assert call("Runtime.evaluate", "ADS")[1]
    assert call("Page.navigate", "CARD", {"url": "https://psp.test/done"})[1]
    assert call("Page.getFrameTree", "CARD")[1] and call("Page.reload", "CARD")[1]
    # Model code can never claim as the runtime, protected or not.
    out, forwarded = call("Hermes.claimPaymentSurface", "ADS", {"frames": ["ADS"], "targets": ["ADS"]})
    assert out["error_type"] == "payment_surface_protected" and not forwarded
    # Control's watcher saw the card frame navigate away: the gate lifts here too.
    sup.status = {"protected": False, "native": False, "frames": [], "targets": []}
    assert call("Runtime.evaluate", "CARD")[1]


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


def test_the_scope_is_registered_before_the_claim_is_sent():
    # The gate fails closed from the moment the scope is registered: when control confirms and begins
    # protecting the splice, this cache already holds the card frames (defence in depth; no window).
    sup = FakeSup()
    at_claim = {}

    def on(method, params, session):
        if method == "Hermes.claimPaymentSurface":
            with surface._LOCK:
                found = surface._SURFACES.get(sup)
            at_claim["frames"] = set(found.frames) if found else None
        return sup.default(method, params, session)

    sup.on = on
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert at_claim["frames"] == {"CARD", "NAMEF"}, "scope must be registered before the claim call"


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
    if change == "lifted":
        sup.status = {"protected": False, "native": False, "frames": [], "targets": []}
    assert surface.payment_click(sup, ref)["error_type"] == error_type
    assert not sup.sent("Input.dispatchMouseEvent")


@pytest.mark.parametrize("status", [
    {"protected": True, "native": False, "frames": ["CARD"], "targets": ["CARD"]},  # another connection holds it
    {"protected": True, "frames": ["CARD"], "targets": ["CARD"]},  # control did not say native (malformed)
    RuntimeError("control unreachable"),  # no fresh status: the cached set is not enough to act
    None,  # malformed reply (no result): not fresh either, and the gate keeps the set
])
def test_native_actions_need_a_fresh_native_status(status):
    # Codex A3 r2 hardening: the native actions run only when control confirms, on this very refresh,
    # that this connection is the surface's native owner. Otherwise they refuse before touching the card.
    sup = _claimed()
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    base, before = sup.on, len(sup.calls)

    def on(method, params, session):
        if method == "Hermes.paymentSurfaceStatus":
            if isinstance(status, Exception):
                raise status
            return status
        return base(method, params, session)

    sup.on = on
    for out in (surface.payment_controls(sup), surface.payment_click(sup, ref)):
        assert out["error_type"] == "payment_surface_unavailable" and "did not run" in out["error"]
    assert {c[0] for c in sup.calls[before:]} == {"Hermes.paymentSurfaceStatus"}
    assert "CARD" in surface.protected_frames(sup)


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


_GOOD = {"protected": False, "native": False, "frames": [], "targets": []}


@pytest.mark.parametrize("status", [
    {},  # Codex A3 r3 P1 repro: {"result": {}}
    None,  # no result
    {"protected": False},  # missing keys
    {"protected": None, "native": False, "frames": [], "targets": []},
    {**_GOOD, "protected": "false"},
    {**_GOOD, "protected": 0},
    {**_GOOD, "native": None},
    {**_GOOD, "frames": None},
    {**_GOOD, "frames": "CARD"},
    {**_GOOD, "targets": [1]},
    {**_GOOD, "frames": ["CARD"], "targets": ["CARD"]},  # "unprotected" yet naming frames: contradictory
    {"protected": True, "native": True, "frames": [], "targets": []},  # protected with an empty set
    {"result": _GOOD},  # extra nesting
    [_GOOD],
])
def test_a_malformed_status_never_lifts_protection(routed, status):
    sup, call = routed
    sup.status = status
    out, forwarded = call("Runtime.evaluate", "CARD", {"expression": "document.querySelector('input').value"})
    assert out["error_type"] == "payment_surface_protected" and not forwarded
    assert {"CARD", "NAMEF"} <= surface.protected_frames(sup)
    assert surface.payment_controls(sup)["error_type"] == "payment_surface_unavailable"
    # Only control's valid explicit answer lifts it.
    sup.status = dict(_GOOD)
    assert call("Runtime.evaluate", "CARD")[1]


def _slow_stale_status(sup):
    """Status answers 'unprotected' at once, but the reply reaches _refresh only once `go` is set."""
    asked, go = threading.Event(), threading.Event()
    real = sup._cdp

    async def cdp(method, params=None, *, session_id=None, timeout=10.0):
        if method != "Hermes.paymentSurfaceStatus":
            return await real(method, params, session_id=session_id, timeout=timeout)
        reply = {"id": 0, "result": dict(_GOOD)}
        asked.set()
        await async_wait_until(go.is_set, timeout=5)
        return reply

    sup._cdp = cdp
    return asked, go


def test_a_stale_refresh_racing_a_newer_claim_keeps_the_newer_scope(routed):
    # Codex A3 r3 P1 repro: an "unprotected" status is received, another claim completes, then the old
    # reply is processed. It must not admit the read: the read is decided against the newest scope.
    sup, _call = routed
    asked, go = _slow_stale_status(sup)
    out = {}
    worker = threading.Thread(target=lambda: out.update(r=surface.supervisor_route_refusal(
        sup, "CARD", "Runtime.evaluate", {"expression": "1"}, "S-card")))
    worker.start()
    assert asked.wait(2)
    assert surface.claim_payment_surface(sup, _inspected()) is None
    go.set()
    worker.join(3)
    assert out["r"] and out["r"]["error_type"] == "payment_surface_protected"
    with surface._LOCK:
        assert {"CARD", "NAMEF"} <= surface._SURFACES[sup].frames, "the stale reply cleared nothing"


def test_a_browser_cdp_call_overlapping_a_claim_is_withheld(monkeypatch):
    # Serialized with claims: a read admitted before any card fill, still running when the fill's claim
    # registers (an awaitPromise that resolves later), never returns its value.
    sup = FakeSup()
    frames = [{"frame_id": "PAGE"}, {"frame_id": "CARD", "session_id": "S-card"}]
    sup.snapshot = lambda: SimpleNamespace(frame_tree={"top": frames[0], "children": frames[1:]})
    sup._frames = {f["frame_id"]: SimpleNamespace(cdp_session_id=f.get("session_id")) for f in frames}
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup)
    sup.delays = {"Runtime.evaluate": 0.4}
    out = {}
    worker = threading.Thread(target=lambda: out.update(r=json.loads(cdp_tool._browser_cdp_via_supervisor(
        "t", "CARD", "Runtime.evaluate", {"expression": "card()", "awaitPromise": True}, 5))))
    worker.start()
    wait_until(lambda: sup.sent("Runtime.evaluate"), timeout=2)
    assert surface.claim_payment_surface(sup, _inspected()) is None
    worker.join(3)
    assert out["r"]["error_type"] == "payment_surface_protected" and "result" not in out["r"]
    # Without a claim in between, the same call returns as before.
    sup2 = FakeSup()
    sup2.snapshot, sup2._frames = sup.snapshot, sup._frames
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup2)
    assert json.loads(cdp_tool._browser_cdp_via_supervisor("t", "CARD", "Runtime.evaluate", {}, 5))["success"]
