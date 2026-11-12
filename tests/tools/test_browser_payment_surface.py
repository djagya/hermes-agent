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


def frame_tree(rows):
    """``Page.getFrameTree``'s result for tree rows ``(id, loader, url, origin, parent)``."""
    def node(row):
        return {"frame": {"id": row[0], "loaderId": row[1], "url": row[2]},
                "childFrames": [node(r) for r in rows if r[4] == row[0]]}
    return {"frameTree": node(rows[0])}


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
        self.tree = TREE  # what Page.getFrameTree answers (the document check before a listing and a press)

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
        if method == "Page.getFrameTree":
            return frame_tree(self.tree)
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


def _bump(sup):
    """A claim on ``sup``'s slot started (its epoch moves), as claim_payment_surface does first."""
    with surface._LOCK:
        surface._EPOCHS[sup] = surface._EPOCHS.get(sup, 0) + 1


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


def test_a_claim_reply_with_tab_type_targets_confirms_and_is_never_sent_back():
    # control 8fe2e9fe69: the owning set also holds the tab-type target of each claimed page
    sup = FakeSup()
    base = sup.on
    sup.on = lambda m, p, s: ({"claimed": True, "locked": True, "tabs": sorted(["PAGE", "TABTARGET-PAGE"])}
                              if m == "Hermes.claimPaymentSurface" else base(m, p, s))
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert surface.claim_payment_surface(sup, _inspected()) is None  # an extension
    assert [c[1] for c in sup.sent("Hermes.claimPaymentSurface")] == [{"tabs": ["PAGE"]}, {"tabs": ["PAGE"]}]


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
    before = surface.epoch(sup)
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert at_claim == {"held": True, "claiming": True} and surface.epoch(sup) == before + 1
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
    monkeypatch.setattr(bvp, "_register_card_redaction", lambda secret, supervisor=None: None)
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


def test_a_claim_between_the_lock_check_and_the_send_stops_the_send(routed, monkeypatch):
    # The lock check passed, then a card fill's claim registered before the call went out: an evaluation
    # sent now would run against the card fields, so it is never sent (not only its reply withheld).
    sup, call = routed
    check = surface.supervisor_route_refusal

    def check_then_claim(*args):
        out = check(*args)
        with surface._LOCK:
            surface._EPOCHS[sup] = surface._EPOCHS.get(sup, 0) + 1
        return out

    monkeypatch.setattr(surface, "supervisor_route_refusal", check_then_claim)
    out, forwarded = call("Runtime.evaluate", "ADS", {"expression": "document.querySelector('input').value"})
    assert not forwarded and out["error_type"] == "payment_surface_locked" and "result" not in out


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


def test_payment_controls_mask_digit_runs_and_each_listing_issues_new_refs():
    sup = _claimed()
    out = surface.payment_controls(sup)
    assert out["success"] is True and out["documents"] == 1
    assert [c["role"] for c in out["controls"]] == ["button", "link", "checkbox"]
    assert out["controls"][0]["text"] == "Pay with card •••• •••• •••• ••••"
    assert not any(re.search(r"\d{4}", c["text"]) for c in out["controls"])
    assert all(set(c) == {"ref", "role", "text", "disabled"} for c in out["controls"])
    again = surface.payment_controls(sup)["controls"]
    assert [c["text"] for c in again] == [c["text"] for c in out["controls"]]
    assert not {c["ref"] for c in again} & {c["ref"] for c in out["controls"]}
    # a newer listing kills the older refs
    assert surface.payment_click(sup, out["controls"][0]["ref"])["error_type"] == "payment_control_unknown"
    assert not sup.sent("Input.dispatchMouseEvent")
    # The lister ran in an isolated world of the protected frame, never the page's own world.
    (_, world, session), = sup.sent("Page.createIsolatedWorld")[:1]
    assert world["frameId"] == "CARD" and session == "S-card"


@pytest.mark.parametrize("outcome", ["pressed", "uncertain"])
def test_a_ref_is_single_use_once_a_press_started(outcome):
    sup = _claimed()
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    if outcome == "uncertain":
        base = sup.on

        def on(method, params, session):
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
                raise TimeoutError("lost reply")
            return base(method, params, session)

        sup.on = on
    first = surface.payment_click(sup, ref)
    assert first.get("success") is True if outcome == "pressed" else first["error_type"] == "payment_click_uncertain"
    presses = len([c for c in sup.sent("Input.dispatchMouseEvent") if c[1]["type"] == "mousePressed"])
    assert surface.payment_click(sup, ref)["error_type"] == "payment_control_unknown"
    assert len([c for c in sup.sent("Input.dispatchMouseEvent") if c[1]["type"] == "mousePressed"]) == presses == 1


def test_a_ref_survives_a_click_refused_before_any_press():
    sup = _claimed()
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    sup.hit = False
    assert surface.payment_click(sup, ref)["error_type"] == "payment_control_covered"
    sup.hit = True
    assert surface.payment_click(sup, ref)["success"] is True


@pytest.mark.parametrize("when", ["listing", "press"])
def test_refs_never_act_on_a_replacement_document(when):
    # Codex P1: navigating while the isolated world is created (or before the press) must not let a ref of the
    # old document act on the new one.
    sup = _claimed()
    replaced = (("PAGE", "LP", "https://shop.test/", "https://shop.test", ""),
                ("CARD", "L2", "https://bank.test/acs", "https://bank.test", "PAGE"))
    if when == "listing":
        base = sup.on

        def on(method, params, session):
            if method == "Page.createIsolatedWorld":
                sup.tree = replaced  # the frame now holds another document
            return base(method, params, session)

        sup.on = on
        out = surface.payment_controls(sup)
        assert out["error_type"] == "payment_surface_unavailable" and "controls" not in out
        return
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    base = sup.on

    def on(method, params, session):
        if method == "DOM.getContentQuads":
            sup.tree = replaced  # navigates after the listing matched, before the press
        return base(method, params, session)

    sup.on = on
    out = surface.payment_click(sup, ref)
    assert out["error_type"] == "payment_surface_unavailable" and "nothing was clicked" in out["error"]
    assert not [c for c in sup.sent("Input.dispatchMouseEvent") if c[1]["type"] == "mousePressed"]


@pytest.mark.parametrize("change", ["document", "moved", "covered"])
def test_a_change_during_the_move_never_gets_the_press(change):
    # Codex r2 P1: the hover move can re-render or navigate; everything is read again after it, no press on change
    sup = _claimed()
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    base = sup.on

    def on(method, params, session):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseMoved":
            if change == "document":
                sup.tree = (TREE[0], ("CARD", "L2", "https://bank.test/acs", "https://bank.test", "PAGE"))
            elif change == "moved":
                sup.quad_shift = 40
            else:
                sup.hit = False
        if method == "DOM.getContentQuads" and getattr(sup, "quad_shift", 0):
            return {"quads": [[x + sup.quad_shift for x in [10, 10, 30, 10, 30, 30, 10, 30]]]}
        return base(method, params, session)

    sup.on = on
    out = surface.payment_click(sup, ref)
    assert out["error_type"] in {"payment_surface_unavailable", "payment_control_changed", "payment_control_covered"}
    assert not [c for c in sup.sent("Input.dispatchMouseEvent") if c[1]["type"] == "mousePressed"]
    # nothing was pressed: the ref stays usable
    sup.on, sup.hit, sup.quad_shift, sup.tree = base, True, 0, TREE
    assert surface.payment_click(sup, ref)["success"] is True


def test_payment_control_captions_never_carry_card_values():
    # r7 P1: _DESCRIBE folds aria-label / value / innerText / title into one caption, so one redaction
    # point covers them all. A page echoing the card into a caption must not pass it on through
    # payment_controls or payment_click, and the ref binds the masked text, never the raw echo.
    from tools import browser_payment_done as done

    sup = _claimed()
    done.register_dialog_card_values({"card_number": "4000056655665556", "cvc": "737", "exp_month": "03",
                                      "exp_year": "2031", "cardholder_name": "Ada Testperson"}, sup)
    rows = [{"i": 0, "role": "button", "text": "Pay with CVC 737; expiry 03/31; Ada Testperson",
             "disabled": False}]
    base = sup.on

    def on(method, params, session):
        if method == "Runtime.callFunctionOn" and params.get("functionDeclaration") == surface._DESCRIBE:
            return {"result": {"value": rows}}
        return base(method, params, session)

    sup.on = on
    secrets = ("737", "03/31", "Ada Testperson")
    out = surface.payment_controls(sup)
    assert out["success"] is True and not any(s in json.dumps(out) for s in secrets)
    ref, text = out["controls"][0]["ref"], out["controls"][0]["text"]
    assert surface._ISSUED[sup][ref][3] == text  # the spec (what a click matches) holds the masked caption
    clicked = surface.payment_click(sup, ref)
    assert clicked["success"] is True and clicked["text"] == text
    assert not any(s in json.dumps(clicked) for s in secrets)


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


def test_claims_are_serialized_and_a_refusal_never_clears_another_claims_hold():
    # Codex P1: a second claim must never run beside the first, and a refused claim must not reopen the gate
    # while another claim is still outstanding.
    sup = FakeSup()
    sup.delays = {"Hermes.claimPaymentSurface": 0.1}  # a slow claim on the wire (awaited: the loop stays free)
    seen = {"now": 0, "max": 0}
    real = sup._cdp

    async def cdp(method, params=None, *, session_id=None, timeout=10.0):
        if method != "Hermes.claimPaymentSurface":
            return await real(method, params, session_id=session_id, timeout=timeout)
        seen["now"] += 1
        seen["max"] = max(seen["max"], seen["now"])
        try:
            return await real(method, params, session_id=session_id, timeout=timeout)
        finally:
            seen["now"] -= 1

    sup._cdp = cdp
    outs = []
    threads = [threading.Thread(target=lambda: outs.append(surface.claim_payment_surface(sup, _inspected())))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert outs == [None, None, None] and seen["max"] == 1
    # an outstanding claim (counted) survives control's refusal of another one
    fresh = FakeSup()
    fresh.on = lambda method, params, session: (_ for _ in ()).throw(RuntimeError(
        "CDP error on id=3: payment_surface_unavailable: held")) if method == "Hermes.claimPaymentSurface" \
        else fresh.default(method, params, session)
    with surface._LOCK:
        surface._CLAIMING[fresh] = 1
    try:
        assert surface.claim_payment_surface(fresh, _inspected())["error_type"] == "payment_surface_unavailable"
        with surface._LOCK:
            assert fresh in surface._HELD and surface._CLAIMING.get(fresh) == 1
        assert surface.slot_locked(fresh) is True
    finally:
        with surface._LOCK:
            surface._CLAIMING.pop(fresh, None)


def test_a_status_read_straddling_a_claim_is_never_a_release(monkeypatch):
    # Codex P2: an "unlocked" answer to a read that a claim overlapped proves nothing.
    from tools import browser_payment_done as done

    sup = FakeSup()

    def on(method, params, session):
        if method == "Hermes.paymentSurfaceStatus":
            _bump(sup)  # a claim started while the read was on the wire
            return dict(UNLOCKED)
        return sup.default(method, params, session)

    sup.on = on
    assert surface.lock_status(sup) == "locked"
    monkeypatch.setattr(done, "_RELEASE_WAIT_S", 0.05)
    monkeypatch.setattr(done, "_RELEASE_POLL_S", 0.01)
    assert done._released(sup) is False
    sup.on = sup.default  # a clean read
    assert surface.lock_status(sup) == "unlocked" and done._released(sup) is True


def test_dialog_answers_wait_for_claims_and_withhold_across_one(monkeypatch):
    from tools import browser_dialog_tool, browser_exec_dialogs

    sup = FakeSup()
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup)
    order = []

    def respond(*a, **k):
        order.append("answer")
        if k.get("prompt_text") == "claim":
            _bump(sup)  # another claim started while this answer ran
        return {"ok": True, "dialog": {"id": "d1", "message": "Order 4242 total"}}

    sup.respond_to_dialog = respond
    # a claim started while an answer runs waits for it (the claim lock), then proceeds
    lock = surface._claim_lock(sup)
    with lock:
        claimer = threading.Thread(target=lambda: order.append(("claim", surface.claim_payment_surface(sup, _inspected()))))
        claimer.start()
        order.append("answer")  # stands in for the answer that holds the lock
    claimer.join(10)
    assert order == ["answer", ("claim", None)]
    # across a claim the payload never reaches the model
    sup.status = dict(UNLOCKED)
    with surface._LOCK:
        surface._HELD.discard(sup)
    out = json.loads(browser_dialog_tool.browser_dialog("accept", prompt_text="claim"))
    assert out["error_type"] == "payment_surface_locked" and "4242" not in json.dumps(out)
    out = browser_exec_dialogs.respond("t", action="accept", prompt_text="claim")
    assert out["error_type"] == "payment_surface_locked" and "dialog" not in out


def test_vault_page_ops_withhold_their_payload_across_a_claim(monkeypatch):
    from tools import browser_vault_tool as vault

    sup = FakeSup()
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup)

    def op():
        _bump(sup)
        return json.dumps({"success": True, "page": "Order 4242"})

    out = json.loads(vault._outside_payment_lock("t", op))
    assert out["error_type"] == "payment_surface_locked" and "4242" not in json.dumps(out)
    assert json.loads(vault._outside_payment_lock("t", lambda: json.dumps({"success": True})))["success"] is True


def test_dialog_automation_stands_down_while_locked(monkeypatch):
    from tools import browser_exec_dialogs
    from tools import browser_supervisor as bs
    from tools.browser_supervisor_dialogs import DIALOG_POLICY_AUTO_ACCEPT

    sup = bs.CDPSupervisor(task_id="locked-dialogs", cdp_url="ws://127.0.0.1:9222", dialog_policy=DIALOG_POLICY_AUTO_ACCEPT)
    sent = []

    async def cdp(method, params=None, *, session_id=None, timeout=10.0):
        sent.append((method, params or {}, session_id))
        return {"result": {"targetInfos": [{"targetId": "T2", "type": "page"}], "sessionId": "S2"}}

    sup._cdp = cdp
    with surface._LOCK:
        surface._HELD.add(sup)

    def answers():
        return [(m, p.get("accept")) for m, p, _s in sent if m == "Page.handleJavaScriptDialog"]

    async def scenario():
        # locked: a confirm is held (the configured auto_accept policy does not apply), the policy is the lock's
        sup._admit_dialog(type="confirm", message="Place order 4242?", default_prompt="", session_id="S1",
                          frame_id=None)
        await asyncio.sleep(0)
        (dialog_id,) = list(sup._pending_dialogs)
        assert await sup._watch_dialog_tabs() == 0
        await sup._watch_tab("T3")
        await sup._install_dialog_bridge("S1")
        assert sent == []  # no attach, no answer, no bridge script or Fetch on a locked slot
        # an alert is accepted and a prompt dismissed natively, at once
        sup._admit_dialog(type="alert", message="Saved", default_prompt="", session_id="S1", frame_id=None)
        sup._admit_dialog(type="prompt", message="Type the code", default_prompt="x", session_id="S1", frame_id=None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert answers() == [("Page.handleJavaScriptDialog", True), ("Page.handleJavaScriptDialog", False)]
        # the watchdog dismisses the held confirm, so it never stalls the page for good
        await sup._dialog_timeout_expired(dialog_id)
        assert dialog_id not in sup._pending_dialogs and answers()[-1] == ("Page.handleJavaScriptDialog", False)
        sent.clear()
        # an answer decided before the lock is re-decided at dispatch: a confirm goes back to pending
        with surface._LOCK:
            surface._HELD.discard(sup)
        sup._admit_dialog(type="confirm", message="Again?", default_prompt="", session_id="S1", frame_id=None)
        with surface._LOCK:
            surface._HELD.add(sup)  # locked before the queued auto answer ran
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert answers() == [] and [d.message for d in sup._pending_dialogs.values()] == ["Again?"]
        with surface._LOCK:
            surface._HELD.discard(sup)
        sup._admit_dialog(type="confirm", message="Saved?", default_prompt="", session_id="S1", frame_id=None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert answers() == [("Page.handleJavaScriptDialog", True)]  # unlocked: the configured policy again
        for handle in sup._dialog_watchdogs.values():
            handle.cancel()

    asyncio.run(scenario())
    # browser_exec installs no policy and watches no tab while locked
    locked = FakeSup()
    locked.watch_dialog_tabs = lambda: pytest.fail("tabs watched on a locked slot")
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: locked)
    assert surface.claim_payment_surface(locked, _inspected()) is None
    browser_exec_dialogs.arm("t", {}, on_dialog="accept", identity="")
    browser_exec_dialogs.disarm("t", {}, identity="")
    assert getattr(locked, "dialog_decider", None) is None


def test_locked_recovery_lists_sites_and_closes_only_this_slots_tabs(monkeypatch):
    from tools import browser_vault_tool as vault

    sup = _claimed()
    pages = [{"targetId": "PAGE", "type": "page", "url": "https://shop.test/checkout?order=1#x", "title": "4242"},
             {"targetId": "OTHER", "type": "page", "url": "https://bank.test/acs/abc", "title": "Card 4242"},
             {"targetId": "CARD", "type": "iframe", "url": "https://psp.test/f", "title": ""}]
    base = sup.on
    sup.on = lambda m, p, s: {"targetInfos": pages} if m == "Target.getTargets" else base(m, p, s)
    monkeypatch.setattr("tools.browser_session_cdp.managed_browser", lambda: True)
    monkeypatch.setattr(vault, "_managed_supervisor", lambda task_id, admit=False: sup)
    listed = json.loads(cdp_tool.browser_cdp("Target.getTargets"))
    assert listed["result"]["targetInfos"] == [
        {"targetId": "PAGE", "type": "page", "url": "https://shop.test", "title": ""},
        {"targetId": "OTHER", "type": "page", "url": "https://bank.test", "title": ""}]
    for params in ({"targetId": "CARD"}, {"targetId": "GONE"}, {"targetId": "PAGE", "x": 1}, None):
        assert json.loads(cdp_tool.browser_cdp("Target.closeTarget", params))["error_type"] == "payment_surface_locked"
    assert not sup.sent("Target.closeTarget")
    assert json.loads(cdp_tool.browser_cdp("Target.closeTarget", {"targetId": "PAGE"}))["success"] is True
    assert [c[1] for c in sup.sent("Target.closeTarget")] == [{"targetId": "PAGE"}]
    # anything else, and every call once the slot is unlocked, keeps the managed refusal
    assert "scope_denied" in cdp_tool.browser_cdp("Runtime.evaluate", {"expression": "1"})
    sup.status = dict(UNLOCKED)
    with surface._LOCK:
        surface._HELD.discard(sup)
    assert "scope_denied" in cdp_tool.browser_cdp("Target.getTargets")


def test_a_beforeunload_is_accepted_only_during_a_handover_close():
    from tools import browser_supervisor as bs

    sup = bs.CDPSupervisor(task_id="locked-unload", cdp_url="ws://127.0.0.1:9222")
    sent = []

    async def cdp(method, params=None, *, session_id=None, timeout=10.0):
        sent.append((method, (params or {}).get("accept")))
        return {"result": {}}

    sup._cdp = cdp
    with surface._LOCK:
        surface._HELD.add(sup)

    async def scenario():
        sup._admit_dialog(type="beforeunload", message="", default_prompt="", session_id="S1", frame_id=None)
        await asyncio.sleep(0)
        assert sent == [] and len(sup._pending_dialogs) == 1  # outside a handover: held like a confirm
        sup._payment_handover_closing = "S2"  # another tab is being closed: still held
        sup._admit_dialog(type="beforeunload", message="", default_prompt="", session_id="S1", frame_id=None)
        await asyncio.sleep(0)
        assert sent == [] and len(sup._pending_dialogs) == 2
        sup._payment_handover_closing = "S1"
        sup._admit_dialog(type="beforeunload", message="", default_prompt="", session_id="S1", frame_id=None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert sent == [("Page.handleJavaScriptDialog", True)]
        for handle in sup._dialog_watchdogs.values():
            handle.cancel()

    asyncio.run(scenario())


def test_recovery_close_forgets_the_recorded_payment_tab(monkeypatch):
    from tools import browser_payment_done as done

    sup = _claimed()
    base = sup.on
    sup.on = lambda m, p, s: ({"targetInfos": [{"targetId": "PAGE", "type": "page", "url": "https://shop.test/"}]}
                              if m == "Target.getTargets" else base(m, p, s))
    assert done.has_payment(sup)
    assert surface.recovery_call(sup, "Target.closeTarget", {"targetId": "PAGE"})["success"] is True
    assert not done.has_payment(sup)


def test_another_tasks_claim_never_withholds_this_tasks_result(routed):
    # peer r2 P3a: the epoch is per supervisor
    sup, call = routed
    sup.delays = {"Runtime.evaluate": 0.4}
    got = {}
    thread = threading.Thread(target=lambda: got.update(out=call("Runtime.evaluate", "ADS", {"expression": "1"})))
    thread.start()
    wait_until(lambda: sup.sent("Runtime.evaluate"), timeout=5)
    other = FakeSup()
    assert surface.claim_payment_surface(other, _inspected()) is None
    thread.join(5)
    out, forwarded = got["out"]
    assert forwarded and out["success"] is True


def test_vault_dispatches_are_fenced_one_by_one_and_human_waits_hold_no_lock(monkeypatch):
    # Codex r2 P1: a claim during a user prompt (enter_code waits for the code) stops the write that follows
    from tools import browser_vault_tool as vault

    sup = FakeSup()
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup)
    dispatched = []

    def op():
        assert vault._native(sup, lambda: dispatched.append("read") or {"ok": True})["ok"] is True
        assert not surface._claim_lock(sup).locked()  # the "user prompt": no claim lock held
        assert surface.claim_payment_surface(sup, _inspected()) is None  # a card fill claims meanwhile
        out = vault._native(sup, lambda: dispatched.append("write") or {"ok": True})
        assert out == {"ok": False, "error": "payment_surface_locked"}
        return json.dumps({"success": False, "error": "write failed"})

    out = json.loads(vault._outside_payment_lock("t", op))
    assert dispatched == ["read"] and out["error_type"] == "payment_surface_locked"


def test_a_claim_outwaits_a_fenced_dialog_answer(monkeypatch):
    # peer r2 P3e: a card fill is never refused only because a dialog answer held the claim lock
    monkeypatch.setattr(surface, "_FENCE_WAIT_S", 0.1)
    monkeypatch.setattr(surface, "_CLAIM_WAIT_S", 2.0)
    sup = FakeSup()
    entered = threading.Event()
    release = threading.Event()

    def slow_answer():
        entered.set()
        release.wait(5)
        return {"ok": True}

    answer = threading.Thread(target=lambda: surface.fenced_native(sup, slow_answer))
    answer.start()
    assert entered.wait(5)
    claimed = {}
    claimer = threading.Thread(target=lambda: claimed.update(out=surface.claim_payment_surface(sup, _inspected())))
    claimer.start()
    time.sleep(0.4)  # health: allow HX013 -- the answer must hold the lock longer than _FENCE_WAIT_S
    assert claimer.is_alive()  # still waiting, not refused
    release.set()
    answer.join(5)
    claimer.join(5)
    assert claimed["out"] is None
    assert surface._CLAIM_WAIT_S > surface._FENCE_WAIT_S


def test_a_claim_during_a_dialog_tab_attach_stops_the_session_work():
    # Codex r2 P2: continuations after an await are gated at dispatch
    from tools import browser_supervisor as bs

    sup = bs.CDPSupervisor(task_id="attach-race", cdp_url="ws://127.0.0.1:9222")
    sent = []

    async def cdp(method, params=None, *, session_id=None, timeout=10.0):
        sent.append(method)
        if method == "Target.attachToTarget":
            with surface._LOCK:
                surface._HELD.add(sup)  # a card fill claims while the attach is on the wire
            return {"result": {"sessionId": "S9"}}
        return {"result": {}}

    sup._cdp = cdp
    asyncio.run(sup._watch_tab("T9"))
    assert sent == ["Target.attachToTarget", "Target.detachFromTarget"] and "T9" not in sup._dialog_tabs()


def _dialog_sup(task_id, fail=None):
    from tools import browser_supervisor as bs

    sup = bs.CDPSupervisor(task_id=task_id, cdp_url="ws://127.0.0.1:9222")
    sup.sent, sup.gate = [], None

    async def cdp(method, params=None, *, session_id=None, timeout=10.0):
        sup.sent.append((method, (params or {}).get("accept")))
        if sup.gate is not None:
            await sup.gate.wait()
        if fail:
            raise RuntimeError("CDP error")
        return {"result": {}}

    sup._cdp = cdp
    return sup


def test_a_dialog_is_answered_once_by_the_agent_or_the_watchdog():
    sup = _dialog_sup("one-answer")

    async def scenario():
        sup.gate = asyncio.Event()
        sup._admit_dialog(type="confirm", message="Place order?", default_prompt="", session_id="S1", frame_id=None)
        (dialog,) = sup._pending_dialogs.values()
        agent = asyncio.create_task(sup._handle_dialog_cdp(dialog, accept=True, prompt_text=""))
        await asyncio.sleep(0)  # the agent's answer is on the wire
        watchdog = asyncio.create_task(sup._dialog_timeout_expired(dialog.id))  # the watchdog fires meanwhile
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sup.gate.set()
        await asyncio.wait_for(asyncio.gather(agent, watchdog), 5)
        with pytest.raises(RuntimeError):  # a second answer is refused, nothing sent
            await sup._handle_dialog_cdp(dialog, accept=False, prompt_text="")
        assert sup.sent == [("Page.handleJavaScriptDialog", True)] and not sup._dialog_watchdogs

    asyncio.run(scenario())
    failing = _dialog_sup("one-answer-fail", fail=True)

    async def failed_send():
        failing._admit_dialog(type="confirm", message="Pay?", default_prompt="", session_id="S1", frame_id=None)
        (dialog,) = failing._pending_dialogs.values()
        with pytest.raises(RuntimeError):
            await failing._handle_dialog_cdp(dialog, accept=True, prompt_text="")
        assert not failing._pending_dialogs and not failing._dialog_watchdogs  # consumed although it failed

    asyncio.run(failed_send())


def test_a_closed_tab_drops_its_held_dialogs():
    sup = _dialog_sup("closed-tab")
    sup._dialog_tab_sessions = {"T2": "S2"}

    async def scenario():
        for sid in ("S1", "S2", "S3"):
            sup._admit_dialog(type="confirm", message="Pay?", default_prompt="", session_id=sid, frame_id=None)
        sup._on_target_detached({"sessionId": "S1"}, None)
        assert sup._dialog_tab_event_ignored("Target.detachedFromTarget", {"sessionId": "S2"}, None)
        assert [d.cdp_session_id for d in sup._pending_dialogs.values()] == ["S3"]
        assert len(sup._dialog_watchdogs) == 1 and sup.sent == []  # dropped, never answered
        for handle in sup._dialog_watchdogs.values():
            handle.cancel()

    asyncio.run(scenario())


def test_dialog_text_from_the_lock_is_never_shown_after_it_and_card_values_are_redacted(monkeypatch):
    from tools import browser_exec_dialogs
    from tools import browser_payment_done as done

    sup = _dialog_sup("after-lock")
    done.register_dialog_card_values({"card_number": "4000056655665556", "cvc": "737", "exp_month": "03",
                                      "exp_year": "2031", "cardholder_name": "Ada Testperson"}, sup)
    monkeypatch.setattr(browser_exec_dialogs, "_supervisor", lambda task_id: sup)

    async def scenario():
        with surface._LOCK:
            surface._HELD.add(sup)
        sup._admit_dialog(type="confirm", message="Echo 4000056655665556 ok?", default_prompt="", session_id="S1",
                          frame_id=None)
        with surface._LOCK:
            surface._HELD.discard(sup)
        sup._admit_dialog(type="confirm", message="Ada Testperson, 03/31, CVC 737?", default_prompt="",
                          session_id="S1", frame_id=None)
        for handle in sup._dialog_watchdogs.values():
            handle.cancel()

    asyncio.run(scenario())
    out = browser_exec_dialogs.collect("after-lock", 0)
    locked, unlocked = out["pending"]
    assert locked["withheld"] == "payment_surface_locked" and "message" not in locked
    assert unlocked["message"] == "«redacted-card», ••/••, CVC •••?"
    blob = json.dumps(sup.snapshot().to_dict())
    assert "4000056655665556" not in blob and "Echo" not in blob and "Testperson" not in blob


def _plain_dialog(monkeypatch, task, message, card=None):
    """A held confirm on a supervisor with no lock state, admitted and collected through browser_exec."""
    from tools import browser_exec_dialogs
    from tools import browser_payment_done as done

    sup = _dialog_sup(task)
    if card:
        done.register_dialog_card_values(card, sup)
    monkeypatch.setattr(browser_exec_dialogs, "_supervisor", lambda task_id: sup)

    async def scenario():
        sup._admit_dialog(type="confirm", message=message, default_prompt="", session_id="S1", frame_id=None)
        for handle in sup._dialog_watchdogs.values():
            handle.cancel()

    asyncio.run(scenario())
    return sup, browser_exec_dialogs.collect(task, 0)


def test_ordinary_dialog_text_keeps_its_numbers_and_dates_without_payment_exposure(monkeypatch):
    # r7 P2 (owner decision): the digit/date/amount mask rides on payment exposure, not on every dialog.
    # A fresh supervisor's confirm stays verifiable, and a capture-guard mark on ANOTHER slot does not
    # reach into this one.
    from tools import browser_use_cli
    from tools.browser_payment_privacy import mark_payment_session

    mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: {"slot_id": "research-1"})
    _sup, out = _plain_dialog(monkeypatch, "plain-dialogs", "Delete invoice 2024-118 dated 12/2025?")
    assert out["pending"][0]["message"] == "Delete invoice 2024-118 dated 12/2025?"


def test_a_dialog_on_a_slot_that_filled_a_card_is_masked_as_today(monkeypatch):
    # the other half of the owner decision: a registered card keeps the blanket mask on that slot
    _sup, out = _plain_dialog(monkeypatch, "card-dialogs", "Delete invoice 2024-118 dated 12/2025?",
                              card={"card_number": "4000056655665556", "cvc": "737", "exp_month": "03",
                                    "exp_year": "2031", "cardholder_name": "Ada Testperson"})
    assert out["pending"][0]["message"] == "Delete invoice ••••-••• dated ••/••••?"


def test_a_capture_guard_mark_on_this_slot_masks_its_dialogs_and_a_fresh_supervisor_keeps_the_holder(
        monkeypatch):
    # the persistent mark outlives the payment: the slot's dialogs stay masked until an operator clears it.
    # r8 (Codex P2): the holder values live per slot, so a replacement supervisor still scrubs them; with
    # none in memory (a restart) the text is withheld, since the holder could not be scrubbed.
    from tools import browser_payment_card_values as card_values
    from tools import browser_use_cli
    from tools.browser_payment_privacy import mark_payment_session

    mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: {"slot_id": "shopping"})
    _sup, out = _plain_dialog(monkeypatch, "marked-dialogs", "Delete invoice 2024-118 dated 12/2025?")
    assert out["pending"][0]["message"] == "[dialog text withheld: payment exposure]"
    old = _dialog_sup("old-supervisor")
    card_values.register_dialog_card_values({"card_number": "4000056655665556", "cvc": "737",
                                             "cardholder_name": "Ada Testperson"}, old)
    try:
        _sup, out = _plain_dialog(monkeypatch, "fresh-supervisor", "Holder Ada Testperson, invoice 2024-118?")
        assert out["pending"][0]["message"] == "Holder «redacted-card», invoice ••••-•••?"
    finally:
        card_values.release_slot("shopping")


@pytest.mark.parametrize("probe", ["Delete invoice 737 or 738?", "Pick 736 737 738"])
def test_card_digits_leave_no_oracle_in_tool_output_or_another_slots_dialogs(monkeypatch, probe):
    # r8 (Codex P1, peer P2): a profile-wide exact scrub of the CVC answered "is it 737?" in any output
    # and in an ordinary dialog on an unmarked slot. Registered 737 and 738 now give the same text.
    from agent import redact
    from tools import browser_use_cli
    from tools import browser_vault_payment as bvp

    monkeypatch.setattr(browser_use_cli, "vault_route",
                        lambda task_id: {"slot_id": "shopping" if task_id == "checkout" else "research"})
    outs = []
    try:
        for cvc in ("737", "738"):
            redact.clear_vault_redaction_values()
            bvp._register_card_redaction({"card_number": "4000056655665556", "cvc": cvc,
                                          "cardholder_name": "Ada Testperson"}, _dialog_sup("checkout"))
            _sup, out = _plain_dialog(monkeypatch, f"research-{cvc}", probe)
            outs.append((out["pending"][0]["message"], redact.redact_terminal_output(probe, force=True),
                         redact.redact_sensitive_text(probe, code_file=True)))
    finally:
        redact.clear_vault_redaction_values()
    assert outs[0] == outs[1] and "737" not in str(outs[0]), outs


def test_a_short_holder_is_scrubbed_from_captions_and_dialogs():
    # r8 (Codex P2): a two-letter holder ("Li") was never registered, so "Pay with Li" reached the model
    from tools import browser_payment_done as done

    sup = _dialog_sup("short-holder")
    done.register_dialog_card_values({"card_number": "4000056655665556", "cvc": "737", "cardholder_name": "Li"}, sup)
    assert surface._caption("Pay with Li", sup) == "Pay with «redacted-card»"
    assert done._dialog_text("LI, pay now?", sup) == "«redacted-card», pay now?"


def test_a_caption_is_withheld_when_the_redactor_cannot_run(monkeypatch):
    # r8 (peer P3-2): the old fallback masked only runs of 4+ digits, so a CVC, expiry and holder passed
    from tools import browser_payment_done as done

    def boom(text, sup=None):
        raise RuntimeError("no redactor")

    monkeypatch.setattr(done, "redact_dialog_text", boom)
    assert surface._caption("Pay with CVC 737; expiry 03/31; Ada Testperson", object()) == "[caption withheld]"


def test_a_supervisor_without_a_slot_is_exposed_only_while_a_payment_mark_exists(monkeypatch):
    # r8 (Codex P2, peer P3-3): an empty route made every dialog look exposed even with no mark at all
    from tools import browser_use_cli
    from tools.browser_payment_done import payment_text_exposed
    from tools.browser_payment_privacy import mark_payment_session

    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: {})
    _sup, out = _plain_dialog(monkeypatch, "unrouted", "Delete invoice 2024-118 dated 12/2025?")
    assert out["pending"][0]["message"] == "Delete invoice 2024-118 dated 12/2025?"
    mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    assert payment_text_exposed(_dialog_sup("unrouted-2"))


def test_timed_out_supervisor_work_is_cancelled_and_drained_before_the_caller_moves_on():
    from tools import browser_supervisor as bs

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    events = []

    async def work():
        try:
            await asyncio.Event().wait()  # a CDP reply that never comes
            events.append("sent")
        finally:
            for _ in range(3):  # cleanup that itself awaits
                await asyncio.sleep(0)
            events.append("finished")

    with pytest.raises(Exception):  # health: allow BLE001 -- the timeout type is concurrent.futures'
        bs._schedule(work(), loop, timeout=0.05)
    # drained: nothing of it still runs once the caller (and any fence it held) moved on
    assert events == ["finished"]
    loop.call_soon_threadsafe(loop.stop)


def test_focus_page_sends_nothing_more_once_the_slot_locks():
    sup = _dialog_sup("focus-locked")
    probes = []

    async def attach(target_id, *, timeout, deadline=None, refuse_locked=False):
        assert refuse_locked
        probes.append(target_id)
        with surface._LOCK:
            surface._HELD.add(sup)  # a claim landed while this tab was being attached
        return "S-" + target_id

    async def cdp(method, params=None, *, session_id=None, timeout=10.0):
        sup.sent.append((method, session_id))
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [{"targetId": t, "type": "page", "url": "https://shop.test/"}
                                               for t in ("A", "B")]}}
        return {"result": {}}

    sup._cdp, sup._attach_page = cdp, attach
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    sup._loop = loop
    try:
        out = sup.focus_page("https://shop.test", timeout=30)
    finally:
        with surface._LOCK:
            surface._HELD.discard(sup)
        loop.call_soon_threadsafe(loop.stop)
    assert out["ok"] is False and "payment_surface_locked" in out["error"]
    assert probes == ["A"] and [m for m, _s in sup.sent] == ["Target.getTargets", "Target.detachFromTarget"]
    assert sup._page_session_id != "S-A"


def test_the_dialog_bridge_intercepts_first_and_falls_back_to_native_dialogs():
    from tools import browser_supervisor_dialogs as bsd

    # interception first; without it no override is installed
    for fail in (False, True):
        sup = _dialog_sup("bridge-order")
        calls = []

        async def cdp(method, params=None, *, session_id=None, timeout=10.0, fail=fail):
            calls.append(method)
            if fail and method == "Fetch.enable":
                raise RuntimeError("CDP error")
            return {"result": {}}

        sup._cdp = cdp
        asyncio.run(sup._install_dialog_bridge("S1"))
        assert calls == (["Fetch.enable"] if fail else
                         ["Fetch.enable", "Page.addScriptToEvaluateOnNewDocument", "Runtime.evaluate"])
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the bridge script")
    # an unanswered bridge request (no interception, failed fulfil) opens the browser's own dialog, never "false"
    harness = """
const calls = [];
let mode = 'fail';
global.window = {confirm: (m) => { calls.push('native:' + m); return 'NATIVE'; },
                 alert: (m) => { calls.push('native-alert:' + m); }, prompt: () => 'NATIVE-P'};
global.XMLHttpRequest = class { open() {} send() { if (mode === 'fail') throw new Error('net');
  this.status = 200; this.responseText = JSON.stringify({accept: true}); } };
global.URLSearchParams = class { constructor(o) { this.o = o; } toString() { return ''; } };
BRIDGE
const out = {failed: window.confirm('Place order?')};
window.alert('Saved');
mode = 'ok';
out.answered = window.confirm('Place order?');
out.calls = calls;
console.log(JSON.stringify(out));
""".replace("BRIDGE", bsd._DIALOG_BRIDGE_SCRIPT)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"failed": "NATIVE", "answered": True,
                                      "calls": ["native:Place order?", "native-alert:Saved"]}
