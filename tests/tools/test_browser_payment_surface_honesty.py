"""Build C (2026-11-18 vault-fills hunt): the payment surface reports what it knows. C1 a claim's own tab-type holder
is not "another tab in the lock"; C2 every claim that begins a fill starts a fresh lock record; C3 a click reports its
structural effect and a covered control the occluder's class; C4 a lost native socket is payment_native_lost; C5 an
unknown lock state with no claim is browser_connection_unavailable; C6 two merchant tabs are a typed refusal. Fake
supervisor and fake CDP only; synthetic data."""

import asyncio
import logging

import pytest

from agent.vault_payment_policy import PaymentDestinationError, PaymentPolicyError
from tools import browser_payment_surface as surface
from tests.tools.test_browser_payment_surface import (
    APPROVED,
    UNLOCKED,
    FakeSup,
    _inspected,
)

HOLDER = "TABTARGET-PAGE"


def _holder(tab):
    """The tab-type target Chrome lists as holding page ``tab`` (one per browser tab)."""
    return "TABTARGET-" + tab


@pytest.fixture(autouse=True)
def _unmanaged_fast(monkeypatch):
    monkeypatch.setattr(surface, "_managed", lambda: False)
    monkeypatch.setattr(surface, "_SETTLE_S", 0.05)
    monkeypatch.setattr(surface, "_SETTLE_POLL_S", 0.01)


class HolderSup(FakeSup):
    """Control of 8fe2e9fe69 and later: the claim reply's tabs carry each claimed page's tab-type holder.
    ``holders``: what Hermes.paymentSurfaceHolders answers (an exception: an older control blocks the method)."""

    def __init__(self, holders=None, lock_extra=()):
        super().__init__()
        self.lock = set(lock_extra)
        self.holders = holders

    def default(self, method, params, session):
        if method == "Hermes.claimPaymentSurface":
            self.claimed = list(params["tabs"])
            self.lock |= set(params["tabs"]) | {_holder(t) for t in params["tabs"]}
            self.status = {"locked": True, "native": True, "tabs": sorted(self.lock), "unverified": False}
            return {"claimed": True, "locked": True, "tabs": sorted(self.lock)}
        if method == "Hermes.paymentSurfaceHolders":
            answer = (self.holders if self.holders is not None
                      else {"tabs": self.claimed, "holders": [_holder(t) for t in self.claimed]})
            if isinstance(answer, Exception):
                raise answer
            return answer
        return super().default(method, params, session)


BLOCKED = RuntimeError("CDP error on id=4: {'message': 'cdp_method_blocked: browser-control does not forward this "
                       "method'}")


# ------------------------------------------------------------------ C1


def test_the_claims_own_tab_holder_keeps_the_press_count_known():
    sup = HolderSup()
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert surface.presses_in_lock(sup) == 0, "a fresh claim: no press yet, and this process saw the whole lock"
    assert [c[1] for c in sup.sent("Hermes.claimPaymentSurface")] == [{"tabs": ["PAGE"]}], "the claim is unchanged"


@pytest.mark.parametrize("holders", [
    BLOCKED,  # an older control: today's fail-closed reading
    RuntimeError("timeout"),
    {"tabs": ["PAGE"]},  # malformed
    {"tabs": ["OTHER"], "holders": [HOLDER]},  # another claim's answer
    {"tabs": ["PAGE"], "holders": [HOLDER, "NOT-IN-LOCK"]},  # names an id the lock does not hold
    {"tabs": ["PAGE"], "holders": ["PAGE"]},  # the claimed page itself
    {"tabs": ["PAGE"], "holders": [HOLDER], "extra": 1},
])
def test_an_unnamed_or_malformed_holder_answer_leaves_the_count_unknown(holders):
    sup = HolderSup(holders=holders)
    assert surface.claim_payment_surface(sup, _inspected()) is None  # the fill goes on: only the count is unknown
    assert surface.presses_in_lock(sup) is None


def test_another_page_in_the_lock_is_still_unknown_with_holders_named():
    sup = HolderSup(lock_extra={"POPUP"})
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert surface.presses_in_lock(sup) is None


def test_no_holder_query_when_the_lock_is_only_the_claimed_tab():
    sup = FakeSup()  # control without tab-type targets: the reply is exactly the claimed tab
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert not sup.sent("Hermes.paymentSurfaceHolders") and surface.presses_in_lock(sup) == 0


# ------------------------------------------------------------------ C2


def _press(sup):
    ref = surface.payment_controls(sup)["controls"][0]["ref"]
    assert surface.payment_click(sup, ref)["success"] is True


def test_a_new_fill_after_an_unproven_release_starts_a_fresh_record(caplog):
    """Restart class: the first payment tab is gone at control, but this process never read the unlock. A new fill on
    a new tab must not inherit the old record's presses (or its approval)."""
    sup = HolderSup()
    assert surface.claim_payment_surface(sup, _inspected()) is None
    surface.record_approved_total(sup, *APPROVED)
    _press(sup)
    assert surface.presses_in_lock(sup) == 1
    sup.lock = set()  # control released the first tab; no status was read here
    with caplog.at_level(logging.INFO, logger=surface.logger.name):
        assert surface.claim_payment_surface(sup, _inspected("PAGE2")) is None
    assert surface.presses_in_lock(sup) == 0 and surface.approved_total(sup) is None
    assert any("replaces record" in r.getMessage() for r in caplog.records)
    assert all("shop.test" not in r.getMessage() for r in caplog.records), "ids and counts only"


def test_a_refill_while_the_old_lock_still_holds_carries_a_known_count():
    """r2 P7 (Opus LOW5): the same tab, still in control's lock: the old record was known, so its press stays visible
    (never zero); the new fill is a new approval."""
    sup = HolderSup()
    assert surface.claim_payment_surface(sup, _inspected()) is None
    surface.record_approved_total(sup, *APPROVED)
    _press(sup)
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert surface.presses_in_lock(sup) == 1 and surface.approved_total(sup) is None


def test_a_refill_of_an_unknown_record_stays_unknown():
    sup = HolderSup(lock_extra={"POPUP"})  # another page in the lock: the first record is unknown
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert surface.presses_in_lock(sup) is None
    sup.lock = {"PAGE", HOLDER}  # the popup closed; the old lock still holds the tab
    assert surface.claim_payment_surface(sup, _inspected()) is None
    assert surface.presses_in_lock(sup) is None


def test_a_fill_after_an_unconfirmed_claim_is_unknown():
    sup = HolderSup()
    real = sup.on
    sup.on = lambda m, p, s: (_ for _ in ()).throw(RuntimeError("boom")) if m == "Hermes.claimPaymentSurface" \
        else real(m, p, s)
    assert surface.claim_payment_surface(sup, _inspected())["error_type"] == "payment_surface_unavailable"
    sup.on = real
    assert surface.claim_payment_surface(sup, _inspected("PAGE2")) is None
    assert surface.presses_in_lock(sup) is None, "the first claim may have locked (and been pressed in) unseen"


# ------------------------------------------------------------------ C3


ROWS_PREP = [
    {"i": 0, "role": "button", "text": "Continue", "disabled": False},
    {"i": 1, "role": "button", "text": "Pay now", "disabled": False},
    {"i": 2, "role": "checkbox", "text": "I accept the terms", "disabled": False},
]
CONTINUE, PAY, TERMS = 0, 1, 2


def _hosted(page="PAGE"):
    """A payment whose only card document is the provider's hosted frame (cross-origin to the shop): no document in
    the lock but that frame can read the card, so page-controlled counts may leave (r2 P4)."""
    inspected = _inspected(page)
    inspected.documents = inspected.documents[:1]
    return inspected


class EffectSup(FakeSup):
    """Answers the structural reads: the control's state, the overlay count, the top document; ``after`` (applied at
    the press release) changes them. ``rows``: the listed controls."""

    def __init__(self, rows=None):
        super().__init__()
        self.rows = rows or ROWS_PREP
        self.state = {"listed": True, "hit": True, "checked": False, "disabled": False, "busy": False}
        self.overlays, self.loader, self.occluder = 0, "LP", "dialog"
        self.after = {}

    def default(self, method, params, session):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
            for key, value in self.after.items():
                setattr(self, key, value)
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == surface._DESCRIBE:
            return {"result": {"value": self.rows}}
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == surface._CONTROL_STATE:
            return {"result": {"value": dict(self.state)}}
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == surface._OCCLUDER:
            return {"result": {"value": self.occluder}}
        if method == "Runtime.evaluate" and params.get("expression") == surface._OVERLAYS:
            return {"result": {"value": self.overlays}}
        if method == "Page.getFrameTree":
            tree = ((self.tree[0][0], self.loader, *self.tree[0][2:]),) + tuple(self.tree[1:])
            from tests.tools.test_browser_payment_surface import frame_tree

            return frame_tree(tree)
        return super().default(method, params, session)


def _claimed_effect(sup, inspected=None):
    assert surface.claim_payment_surface(sup, inspected or _hosted()) is None
    surface.record_approved_total(sup, *APPROVED)
    return sup


def _ref(sup, index=CONTINUE):
    return surface.payment_controls(sup)["controls"][index]["ref"]


def _list_top(sup):
    """r4: the payment tab's top document is listed too (Target.getTargetInfo names the payment tab), after the card
    frame's controls."""
    real = sup.on
    sup.on = lambda m, p, s: {"targetInfo": {"targetId": "PAGE"}} if m == "Target.getTargetInfo" else real(m, p, s)
    return sup


def _mouse_presses(sup):
    return [c for c in sup.calls if c[0] == "Input.dispatchMouseEvent" and c[1].get("type") == "mousePressed"]


def test_a_click_that_changed_nothing_says_unchanged():
    sup = _claimed_effect(EffectSup())
    out = surface.payment_click(sup, _ref(sup))
    assert out["success"] is True and "pay_pressed" not in out
    assert out["effect"] == {"navigated": False, "control_listed": True, "control_hit": True,
                             "control_state_changed": False, "overlays_before": 0, "overlays_after": 0,
                             "card_frames_before": 1, "card_frames_after": 1, "unchanged": True}


@pytest.mark.parametrize("after, field, value", [
    ({"loader": "LP2"}, "navigated", True),
    ({"overlays": 1}, "overlays_after", 1),
    ({"state": {"listed": False, "hit": False, "checked": False}}, "control_listed", False),
    ({"state": {"listed": True, "hit": False, "checked": False}}, "control_hit", False),
    # r2 P3: a control that only goes disabled or busy made progress too
    ({"state": {"listed": True, "hit": True, "checked": False, "disabled": True}}, "control_state_changed", True),
    ({"state": {"listed": True, "hit": True, "checked": False, "busy": True}}, "control_state_changed", True),
])
def test_a_click_with_a_visible_effect_is_not_unchanged(after, field, value):
    sup = _claimed_effect(EffectSup())
    sup.after = after
    out = surface.payment_click(sup, _ref(sup))
    assert out["success"] is True and out["effect"][field] == value and out["effect"]["unchanged"] is False


def test_an_effect_holds_counts_and_bools_only():
    sup = _claimed_effect(EffectSup())
    sup.after = {"overlays": 2}
    effect = surface.payment_click(sup, _ref(sup))["effect"]
    assert all(type(v) in (bool, int) for v in effect.values())


def test_an_unreadable_effect_is_null_and_the_click_still_succeeded():
    sup = _claimed_effect(EffectSup())
    real = sup.on

    def on(method, params, session):
        if method == "Runtime.evaluate" and params.get("expression") == surface._OVERLAYS and sup.calls and any(
                c[0] == "Input.dispatchMouseEvent" and c[1].get("type") == "mouseReleased" for c in sup.calls):
            raise RuntimeError("Execution context was destroyed")
        return real(method, params, session)

    sup.on = on
    out = surface.payment_click(sup, _ref(sup))
    assert out["success"] is True and out["effect"] is None and surface.presses_in_lock(sup) == 1


class _Clock:
    """A monotonic clock that advances ``step`` s per read: the settle's bound without any wall-clock assertion."""

    def __init__(self, step):
        self.now, self.step = 1000.0, step

    def monotonic(self):
        self.now += self.step
        return self.now


def test_the_settle_is_bounded(monkeypatch):
    """r2 P8 (Codex P3): bounded by the settle's own clock, not by elapsed wall time: a top document that never
    changes ends the settle after at most _SETTLE_S of clock (here 1.5 s at 0.125 s per read), never later."""
    monkeypatch.setattr(surface, "_SETTLE_S", 1.5)
    monkeypatch.setattr(surface, "_SETTLE_POLL_S", 0.001)
    sup = _claimed_effect(EffectSup())
    monkeypatch.setattr(surface, "time", _Clock(0.125))
    loop = asyncio.new_event_loop()
    try:
        polls = len(sup.sent("Page.getFrameTree"))
        before = {"top": ("PAGE", "LP"), "safe": True, "listed": True, "hit": True, "state": (False, False, False),
                  "overlays": 0, "card_frames": 1}
        effect = loop.run_until_complete(surface._effect(sup, "S-page", "el", "S-card", before, "button"))
        # each settle round reads the clock twice (loop test, sleep length): 1.5 s of clock = 6 polls; then the
        # structure read after the settle (frame tree, and the top again around the overlay count)
        assert len(sup.sent("Page.getFrameTree")) - polls == 6 + 2 and effect["unchanged"] is True
    finally:
        loop.close()


@pytest.mark.parametrize("cls, expected", [("dialog", "dialog"), ("fixed_overlay", "fixed_overlay"),
                                           ("other", "other"), ("Cookie banner: accept all", "other")])
def test_a_covered_control_names_only_the_occluder_class(cls, expected):
    sup = _claimed_effect(_list_top(EffectSup()))
    sup.hit, sup.occluder = False, cls
    out = surface.payment_click(sup, _ref(sup, len(ROWS_PREP) + CONTINUE))  # r4: the merchant top document's control
    assert out["error_type"] == "payment_control_covered" and out["occluder"] == expected
    assert set(out) == {"success", "error_type", "error", "occluder"} and "Cookie" not in out["error"]
    assert not _mouse_presses(sup)


def test_an_overlay_over_the_card_iframe_is_refused_without_a_class():
    """r4 (Codex final low 3): a page element over the card iframe still refuses the card frame's control, now without
    an occluder class (none is classified for a control in a hosted card frame)."""
    sup = _claimed_effect(EffectSup())
    sup.at_point = 99  # another page element sits over the card iframe
    real = sup.on

    def on(method, params, session):
        if method == "Runtime.evaluate" and str(params.get("expression", "")).startswith(surface._OCCLUDER_AT):
            return {"result": {"value": "fixed_overlay"}}
        return real(method, params, session)

    sup.on = on
    out = surface.payment_click(sup, _ref(sup))
    assert out["error_type"] == "payment_control_covered" and "occluder" not in out and not _mouse_presses(sup)
    assert not [c for c in sup.calls if c[0] == "Runtime.evaluate"
                and str(c[1].get("expression", "")).startswith(surface._OCCLUDER_AT)]


# ------------------------------------------------------------------ C4


def test_a_replaced_socket_during_the_lock_is_payment_native_lost():
    sup = _claimed_effect(EffectSup())
    ref = _ref(sup)
    sup._ws = object()  # the supervisor redialled: control's native connection was the claim's socket
    sup.status = {"locked": True, "native": False, "tabs": ["PAGE"], "unverified": False}
    out = surface.payment_click(sup, ref)
    assert out["error_type"] == surface.NATIVE_LOST and "payment_close only after" in out["error"]
    assert not [c for c in sup.calls if c[0] == "Input.dispatchMouseEvent"]
    assert surface.payment_controls(sup)["error_type"] == surface.NATIVE_LOST
    from tools import browser_payment_done as done

    assert done.payment_done(sup)["error_type"] == surface.NATIVE_LOST
    assert done.payment_dialog(sup, "d-1", False)["error_type"] == surface.NATIVE_LOST


def test_a_replaced_socket_after_the_lock_ended_is_not_lost():
    sup = _claimed_effect(EffectSup())
    sup._ws = object()
    sup.status = dict(UNLOCKED)
    assert surface.native_lost_refusal(sup) is None


def test_control_naming_another_native_connection_is_lost_on_the_same_socket():
    sup = _claimed_effect(EffectSup())
    sup.status = {"locked": True, "native": False, "tabs": ["PAGE"], "unverified": False}
    assert surface.lock_status(sup) == "locked"
    assert surface.native_lost_refusal(sup)["error_type"] == surface.NATIVE_LOST


def test_a_supervisor_without_a_claim_on_a_foreign_lock_is_lock_foreign_not_none():
    """r2 P6 (Opus LOW4): no record of this process on a slot another connection locked: its own type and text."""
    sup = EffectSup()  # a replaced supervisor or a restarted gateway: no claim of this process
    sup.status = {"locked": True, "native": False, "tabs": ["PAGE"], "unverified": False}
    out = surface.payment_controls(sup)
    assert out["error_type"] == surface.LOCK_FOREIGN and "do not close it unless Danil says so" in out["error"]
    assert surface.payment_click(sup, "c0-0123456789")["error_type"] == "payment_control_unknown"  # no listing
    sup.status = dict(UNLOCKED)
    assert surface.payment_controls(sup)["error_type"] == "payment_surface_none"


def test_a_redial_after_a_known_socket_change_is_status_only_whatever_status_says():
    """r2 P5 (Codex P2): a lock record whose claim socket was replaced decides alone: no status answer (timeout,
    malformed, even locked-native) makes the supervisor attach again."""
    from tools.browser_supervisor import CDPSupervisor

    sup = _claimed_effect(EffectSup())
    sup.task_id = "t"
    sup._ws = object()
    for status in (RuntimeError("no answer"), {"bogus": True},
                   {"locked": True, "native": True, "tabs": ["PAGE"], "unverified": False}):
        sup.status = status
        run = asyncio.run_coroutine_threadsafe(CDPSupervisor._payment_recovery(sup), sup._loop)
        assert run.result(timeout=10) == "lost", status
    assert not sup.sent("Hermes.paymentSurfaceStatus"), "known locally: no status is needed to stay status-only"
    sup.status = {"locked": True, "native": False, "tabs": ["PAGE"], "unverified": False}
    assert surface.native_lost_refusal(sup)["error_type"] == surface.NATIVE_LOST


@pytest.mark.parametrize("managed, status, expected", [
    (True, {"locked": True, "native": False, "tabs": ["PAGE"], "unverified": False}, "foreign"),
    (True, dict(UNLOCKED), None),
    (True, RuntimeError("no answer"), None),  # unknown at startup: attach as before (control refuses if locked)
    (False, {"locked": True, "native": False, "tabs": ["PAGE"], "unverified": False}, None),
])
def test_a_fresh_supervisor_on_a_foreign_lock_starts_status_only(monkeypatch, managed, status, expected):
    from tools.browser_supervisor import CDPSupervisor

    monkeypatch.setattr(surface, "_managed", lambda: managed)
    fresh = FakeSup()  # no record, no payment of this process
    fresh.task_id, fresh.status = "t", status
    run = asyncio.run_coroutine_threadsafe(CDPSupervisor._payment_recovery(fresh), fresh._loop)
    assert run.result(timeout=10) == expected
    assert bool(fresh.sent("Hermes.paymentSurfaceStatus")) is managed


# ------------------------------------------------------------------ C5


@pytest.mark.parametrize("held, seen, expected", [
    (False, False, surface.CONNECTION_UNAVAILABLE),
    (True, False, surface.LOCKED),
    (False, True, surface.LOCKED),
])
def test_an_unknown_lock_state_without_a_claim_is_a_connection_problem(monkeypatch, held, seen, expected):
    monkeypatch.setattr(surface, "_managed", lambda: True)
    sup = FakeSup()
    if held:
        assert surface.claim_payment_surface(sup, _inspected()) is None
    if seen:
        sup.status = {"locked": True, "native": False, "tabs": ["X"], "unverified": False}
        assert surface.lock_status(sup) == "locked"
    sup.status = RuntimeError("status unreachable")
    out = surface.vault_lock_refusal(sup)
    assert out["error_type"] == expected
    if expected == surface.CONNECTION_UNAVAILABLE:
        assert "Retry this once" in out["error"] and "do not call payment_close" in out["error"]
    assert surface.slot_locked(sup) is True, "either way nothing is dispatched"
    called = []
    state, refused = surface.fenced_dispatch(sup, surface.epoch(sup), lambda: called.append(1))
    assert state == "refused" and refused["error_type"] == expected and not called


def test_an_unknown_state_off_the_managed_browser_is_unlocked():
    sup = FakeSup()
    sup.status = RuntimeError("status unreachable")
    assert surface.vault_lock_refusal(sup) is None


# ------------------------------------------------------------------ C6


class _Tabs:
    """The supervisor surface _merchant_tab_session uses."""

    from tools.browser_supervisor_payment import PaymentSupervisionMixin as _mixin

    _merchant_tab_session = _mixin._merchant_tab_session

    def __init__(self, urls):
        self.urls = urls

    async def _cdp(self, method, params=None, *, session_id=None, timeout=10.0):
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [{"targetId": f"T{i}", "type": "page", "url": u}
                                               for i, u in enumerate(self.urls)]}}
        if method == "Target.getTargetInfo":
            return {"result": {"targetInfo": {"targetId": "T0"}}}
        raise AssertionError(method)


def test_two_merchant_tabs_are_a_typed_refusal_with_the_way_on():
    sup = _Tabs(["https://shop.test/cart", "https://shop.test/checkout", "https://other.test/"])
    with pytest.raises(PaymentDestinationError) as caught:
        asyncio.run(sup._merchant_tab_session("https://shop.test", "S"))
    exc = caught.value
    assert exc.error_type == "payment_merchant_tab_ambiguous"
    assert exc.detail == {"merchant_origin": "https://shop.test", "merchant_tabs": 2}
    assert "Close the other tabs on https://shop.test" in str(exc) and "fill again" in str(exc)
    assert "/checkout" not in str(exc) and "/cart" not in str(exc), "the origin only, never a path"


def test_no_merchant_tab_stays_a_policy_refusal():
    with pytest.raises(PaymentPolicyError) as caught:
        asyncio.run(_Tabs(["https://other.test/"])._merchant_tab_session("https://shop.test", "S"))
    assert not isinstance(caught.value, PaymentDestinationError)
    assert asyncio.run(_Tabs(["https://shop.test/c"])._merchant_tab_session("https://shop.test", "S")) == "S"


# ------------------------------------------------------------------ round 2 (Codex + Opus review of build C)


def test_a_second_pay_press_goes_while_no_payment_was_detected():
    """G1 (owner 2026-11-19; replaces r2 P1's one-press refusal): with no payment detected, a fresh listing and a
    press of the same Pay control goes through, the approved total read again before it; owner_go too."""
    from tests.tools.test_browser_payment_surface import _claimed, _total_reads

    sup = _claimed()  # the base probe's scenario: ROWS[0] is "Pay with card ..." on a checked approval
    first = surface.payment_click(sup, surface.payment_controls(sup)["controls"][0]["ref"])
    assert first["success"] is True and first["pay_pressed"] is True and first["payment_detected"] is False
    for owner_go in (False, True):
        listing = surface.payment_controls(sup)
        reads = len(_total_reads(sup))
        second = surface.payment_click(sup, listing["controls"][0]["ref"], owner_go=owner_go)
        assert second["success"] is True and second["pay_pressed"] is True
        assert len(_total_reads(sup)) > reads, "the approved total is read before every press"
    assert len(_mouse_presses(sup)) == 3 and surface.presses_in_lock(sup) == 3
def test_preparatory_clicks_and_pay_presses_go_in_any_order():
    """G1: the terms checkbox and Continue before and after Pay, and Pay again, all go; only Pay-like presses answer
    pay_pressed. A new fill (a new approval) goes on counting."""
    sup = _claimed_effect(EffectSup())
    for index in (TERMS, CONTINUE, PAY, TERMS, CONTINUE, PAY):
        out = surface.payment_click(sup, _ref(sup, index))
        assert out["success"] is True and out.get("pay_pressed", False) is (index == PAY), index
    assert len(_mouse_presses(sup)) == 6 and surface.presses_in_lock(sup) == 6
    _claimed_effect(sup)  # a new card fill in the same lock: a new approval card
    assert surface.payment_click(sup, _ref(sup, PAY))["success"] is True
    assert surface.presses_in_lock(sup) == 7, "r2 P7: the known count carries over into the new record"
@pytest.mark.parametrize("role, caption, pay", [
    ("button", "Pay now", True), ("button", "Place order", True), ("button", "Jetzt kaufen", True),
    ("button", "Zahlungspflichtig bestellen", True), ("button", "Submit", True), ("button", "Confirm", True),
    ("button", "Continue and pay", True), ("button", "", True), ("button", "OK", True),  # unknown: fails closed
    ("button", surface.CAPTION_WITHHELD, True), ("button", "Continue", False), ("button", "Review order", False),
    ("button", "Weiter zur Zahlung", False), ("button", "Continue to PayPal", False), ("link", "Terms", False),
    ("link", "Jetzt bezahlen", True), ("checkbox", "Pay with this card", False), ("radio", "Buy now", False),
])
def test_which_press_is_the_pay_press(role, caption, pay):
    assert surface._pay_press(role, caption) is pay


def test_a_press_is_counted_at_dispatch_and_an_overlapping_click_is_refused(monkeypatch):
    """r2 P2 (Codex P1, Opus LOW2): with the first press's settle paused, a parallel listing already counts it and a
    parallel click (even of another control) is refused before anything is pressed."""
    import threading

    from tests.waiting import wait_until

    release = threading.Event()

    async def paused(*_a, **_k):
        await asyncio.to_thread(release.wait, 10)

    monkeypatch.setattr(surface, "_bounded_effect", paused)
    sup = _claimed_effect(EffectSup())
    ref = _ref(sup, PAY)
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("first", surface.payment_click(sup, ref)), daemon=True)
    worker.start()
    try:
        wait_until(lambda: any(c[0] == "Input.dispatchMouseEvent" and c[1].get("type") == "mouseReleased"
                               for c in list(sup.calls)), what="the first press released")
        assert surface.presses_in_lock(sup) == 1
        listing = surface.payment_controls(sup)
        assert listing["presses_in_lock"] == 1
        for index in (PAY, CONTINUE):
            busy = surface.payment_click(sup, listing["controls"][index]["ref"])
            assert busy["error_type"] == surface.CLICK_BUSY and "nothing was pressed" in busy["error"]
        assert len(_mouse_presses(sup)) == 1
    finally:
        release.set()
        worker.join(timeout=10)
    assert out["first"]["success"] is True and surface.presses_in_lock(sup) == 1
    assert surface.payment_click(sup, listing["controls"][CONTINUE]["ref"])["success"] is True, "the ref survived"


@pytest.mark.parametrize("after", [{"disabled": True}, {"busy": True}, {}])
def test_a_pay_press_never_reads_as_unchanged(after):
    """r2 P3 (Opus HIGH): a Pay button that only goes disabled (or busy, or shows nothing) after the press: the effect
    reports the state change, unchanged is null, and the answer says Pay must not be pressed again."""
    sup = _claimed_effect(EffectSup())
    sup.after = {"state": {"listed": True, "hit": True, "checked": False, "disabled": False, "busy": False, **after}}
    out = surface.payment_click(sup, _ref(sup, PAY))
    assert out["success"] is True and out["pay_pressed"] is True and out["effect"]["unchanged"] is None
    assert out["effect"]["control_state_changed"] is bool(after)
    assert out["next"] == surface.PAY_PRESSED_NEXT and "call payment_done" in out["next"]


def test_an_owner_go_pay_press_never_reads_as_unchanged_either():
    """r2 P3: a fill-only card (no approved total) pressed with the owner's go: not enforced as the approval's one
    press (no pay gate), but its effect is never "unchanged" either."""
    sup = EffectSup()
    assert surface.claim_payment_surface(sup, _hosted()) is None  # no approved total bound
    out = surface.payment_click(sup, _ref(sup, PAY), owner_go=True)
    assert out["success"] is True and out["pay_pressed"] is True and out["effect"]["unchanged"] is None


def test_a_preparatory_click_that_only_disables_itself_is_not_unchanged():
    sup = _claimed_effect(EffectSup())
    sup.after = {"state": {"listed": True, "hit": True, "checked": False, "disabled": True, "busy": False}}
    effect = surface.payment_click(sup, _ref(sup, CONTINUE))["effect"]
    assert effect["control_state_changed"] is True and effect["unchanged"] is False


# r2 P4: the real overlay and occluder scripts on an adversarial page that can read a filled card value (a same-origin
# card field) and encodes its first digit in the number of dialogs it creates, and a bit in what covers a control.
_ADVERSARIAL_DOM = r"""
const filled = '4242424242424242';  // the page reads it from its same-origin card field
const digit = Number(filled[0]);
const el = (o = {}) => Object.assign({nodeType: 1, parentElement: null, matches: () => true,
  closest: () => (o.dialog ? {} : null), getBoundingClientRect: () => ({left: 0, top: 0, width: 100, height: 100}),
  getAttribute: () => null}, o);
const dialogs = Array.from({length: digit}, () => el());
const cover = el({dialog: digit % 2 === 0});
const document = {querySelectorAll: () => dialogs, elementFromPoint: () => cover, body: {}, documentElement: {}};
const getComputedStyle = () => ({visibility: 'visible', display: 'block', opacity: '1', position: 'static'});
const innerWidth = 1000, innerHeight = 800;
"""


def _in_page(expression, this=False):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the page scripts")
    call = f"({expression}).call(el())" if this else expression
    harness = _ADVERSARIAL_DOM + f"process.stdout.write(JSON.stringify({call}));"
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


class PageSup(EffectSup):
    """The overlay and occluder reads answered by the real scripts on the adversarial page (node)."""

    def default(self, method, params, session):
        if method == "Runtime.evaluate" and params.get("expression") == surface._OVERLAYS:
            return {"result": {"value": _in_page(surface._OVERLAYS)}}
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == surface._OCCLUDER:
            return {"result": {"value": _in_page(surface._OCCLUDER, this=True)}}
        return super().default(method, params, session)


def _detail_reads(sup):
    return [c for c in sup.calls if (c[0] == "Runtime.evaluate" and c[1].get("expression") == surface._OVERLAYS)
            or (c[0] == "Runtime.callFunctionOn" and c[1].get("functionDeclaration") == surface._OCCLUDER)]


def test_the_page_scripts_do_carry_what_an_adversarial_page_chooses():
    """The channel exists: the scripts report what the page built (4 dialogs = the first digit; an even digit makes
    the occluder a dialog), so only withholding them closes it."""
    assert _in_page(surface._OVERLAYS) == 4 and _in_page(surface._OCCLUDER, this=True) == "dialog"


@pytest.mark.parametrize("inspected, tree", [
    (_inspected, None),  # a merchant card field (same-origin card document) next to the hosted frame
    (_hosted, (("PAGE", "LP", "https://psp.test/checkout", "https://psp.test", ""),
               ("CARD", "L1", "https://psp.test/f", "https://psp.test", "PAGE"))),  # the top is on the card origin
    (_hosted, (("PAGE", "LP", "https://shop.test/", "https://shop.test", ""),
               ("CARD", "L1", "https://shop.test/card", "https://shop.test", "PAGE"))),  # card frame same-origin
])
def test_page_controlled_metadata_never_leaves_a_lock_whose_documents_can_read_the_card(inspected, tree):
    sup = PageSup()
    if tree:
        sup.tree = sup.listed = tree  # what the press reads, and what the listing walks
    _claimed_effect(sup, inspected())
    sup.after = {"loader": "LP"}
    effect = surface.payment_click(sup, _ref(sup, CONTINUE))["effect"]
    assert set(effect) == {"navigated", "control_listed", "control_hit", "control_state_changed", "unchanged"}
    sup.hit = False  # now covered: the refusal without a class
    covered = surface.payment_click(sup, _ref(sup, CONTINUE))
    assert covered["error_type"] == "payment_control_covered" and "occluder" not in covered
    assert "dialog" not in covered["error"] and not _detail_reads(sup), "the scripts never ran"


def test_a_hosted_only_payment_returns_the_page_counts():
    sup = _claimed_effect(_list_top(PageSup()))
    effect = surface.payment_click(sup, _ref(sup, CONTINUE))["effect"]
    assert effect["overlays_before"] == effect["overlays_after"] == 4 and effect["card_frames_after"] == 1
    sup.hit = False
    assert surface.payment_click(sup, _ref(sup, len(ROWS_PREP) + CONTINUE))["occluder"] == "dialog"


@pytest.mark.parametrize("frames, safe", [
    ([("PAGE", "https://shop.test/", "https://shop.test"), ("CARD", "https://psp.test/f", "https://psp.test")], True),
    ([("PAGE", "https://shop.test/", ""), ("CARD", "https://psp.test/f", "")], True),  # origins from the urls
    ([("PAGE", "https://shop.test/", "https://shop.test")], True),  # an OOPIF card frame is cross-site by isolation
    ([("PAGE", "https://shop.test/", "https://shop.test"), ("CARD", "about:blank", "")], False),  # unknown origin
    ([("CARD", "https://psp.test/f", "https://psp.test")], False),  # the top document is the card document
    ([], False),
])
def test_detail_safe_reads_every_card_frame_against_the_top(frames, safe):
    sup = FakeSup()
    assert surface.claim_payment_surface(sup, _hosted()) is None
    assert surface.detail_safe(sup, frames) is safe
    assert surface.detail_safe(FakeSup(), frames[:1]) is False, "no payment: never"


# ------------------------------------------------------------------ round 3 (owner corrections to round 2)


class _RowsSup(FakeSup):
    """A FakeSup whose listed controls are ``rows`` (change ``rows`` between listings)."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def default(self, method, params, session):
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == surface._DESCRIBE:
            return {"result": {"value": self.rows}}
        return super().default(method, params, session)


def _row(text, role="button", i=0):
    return {"i": i, "role": role, "text": text, "disabled": False}


def _pay_then(rows_after, tree, listed=None):
    """A claimed approval whose Pay was pressed once; then the page shows ``tree`` with ``rows_after`` controls."""
    from tests.tools.test_browser_payment_surface import _claimed

    sup = _claimed(_RowsSup([_row("Pay now")]))
    assert surface.payment_click(sup, surface.payment_controls(sup)["controls"][0]["ref"])["success"] is True
    sup.rows = rows_after
    sup.tree, sup.listed = tree, listed or tree
    return sup


def _again(sup, index=0, owner_go=False):
    from tests.tools.test_browser_payment_surface import _total_reads

    reads = len(_total_reads(sup))
    mouse = len(_mouse_presses(sup))
    out = surface.payment_click(sup, surface.payment_controls(sup)["controls"][index]["ref"], owner_go=owner_go)
    return out, len(_total_reads(sup)) - reads, len(_mouse_presses(sup)) - mouse


BANK_FRAME = ("CARD", "L9", "https://acs.bank.test/challenge", "https://acs.bank.test", "PAGE")
BANK_TOP = ("PAGE", "LB", "https://acs.bank.test/challenge", "https://acs.bank.test", "")
PROVIDER_FRAME = ("CARD", "L9", "https://psp.test/confirm", "https://psp.test", "PAGE")
PROVIDER_TOP = ("PAGE", "LB", "https://psp.test/hosted", "https://psp.test", "")


@pytest.mark.parametrize("top_case", [False, True], ids=["challenge in the card frame", "bank page as top document"])
def test_after_pay_a_bank_challenge_is_pressed_whatever_its_button_says(top_case):
    """r3 Q1: Pay was pressed once; the issuer's 3-D Secure document (a challenge replacing the card frame, or the
    payment tab's top document on another site) has its own "Confirm" / "Pay", the issuer's confirmation and not a
    second charge: pressed, with no merchant total to read."""
    from tests.tools.test_browser_payment_surface import TREE

    for caption in ("Confirm", "Submit", "Pay 100.00 EUR"):
        sup = _pay_then([_row(caption)], (BANK_TOP, TREE[1]) if top_case else (TREE[0], BANK_FRAME))
        out, total_reads, pressed = _again(sup)
        assert out["success"] is True and pressed == 1 and total_reads == 0, (top_case, caption)
        assert surface.presses_in_lock(sup) == 2
        # G1: back on the merchant document with no payment detected, Pay goes again (no press budget)
        sup.tree = sup.listed = TREE
        out, _reads, pressed = _again(sup)
        assert out["success"] is True and pressed == 1, (top_case, caption)


def test_a_bank_page_before_any_press_is_still_a_pay_press():
    """r3 Q1: a bank page is pressed without the merchant total only after an admitted press. G1: no press budget,
    so the merchant's Pay goes after it too."""
    from tests.tools.test_browser_payment_surface import TREE, _claimed

    sup = _claimed(_RowsSup([_row("Continue")]))
    assert surface.payment_click(sup, surface.payment_controls(sup)["controls"][0]["ref"])["success"] is True
    sup.rows = [_row("Confirm")]
    sup.tree = sup.listed = (BANK_TOP, TREE[1])
    assert _again(sup)[0]["success"] is True  # admitted by Continue, so the bank step goes
    sup.tree = sup.listed = TREE
    sup.rows = [_row("Pay now")]
    assert _again(sup)[0]["success"] is True


def test_two_supervisors_do_not_refuse_each_others_clicks(monkeypatch):
    """r3 Q3: payment_click_busy is per browser: with one supervisor's click held in its settle, another supervisor's
    click goes through, and the first one's own second click is still refused."""
    import threading

    from tests.tools.test_browser_payment_surface import _claimed
    from tests.waiting import wait_until

    release, original = threading.Event(), surface._bounded_effect
    first, second = _claimed(_RowsSup([_row("Pay now"), _row("Continue", i=1)])), _claimed(_RowsSup([_row("Pay now")]))

    async def paused(sup, *args, **kwargs):
        if sup is first:
            await asyncio.to_thread(release.wait, 10)
        return await original(sup, *args, **kwargs)

    monkeypatch.setattr(surface, "_bounded_effect", paused)
    refs = [c["ref"] for c in surface.payment_controls(first)["controls"]]
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("first", surface.payment_click(first, refs[0])),
                              daemon=True)
    worker.start()
    try:
        wait_until(lambda: len(_mouse_presses(first)) == 1, what="the first press dispatched")
        assert surface.payment_click(first, refs[1])["error_type"] == surface.CLICK_BUSY
        other = surface.payment_click(second, surface.payment_controls(second)["controls"][0]["ref"])
        assert other["success"] is True and len(_mouse_presses(second)) == 1
    finally:
        release.set()
        worker.join(timeout=10)
    assert out["first"]["success"] is True
    assert surface.payment_click(first, refs[1])["success"] is True  # the busy refusal kept the ref


def _go_pay_then(rows_after, tree):
    """A fill-only card (no approved total) whose Pay was pressed once with the owner's go; then the page shows
    ``tree`` with ``rows_after`` controls."""
    from tests.tools.test_browser_payment_surface import _claimed

    sup = _claimed(_RowsSup([_row("Pay now")]), approved=None)
    assert surface.payment_click(sup, surface.payment_controls(sup)["controls"][0]["ref"], owner_go=True)["success"]
    sup.rows = rows_after
    sup.tree = sup.listed = tree
    return sup


@pytest.mark.parametrize("top_case", [False, True], ids=["challenge in the card frame", "bank page as top document"])
def test_after_an_owner_go_pay_the_bank_step_and_another_pay_are_pressed(top_case):
    """r4 (Codex final P1, Opus L5) / G1: after an owner_go Pay press the issuer's 3-D Secure Confirm is pressed, and
    with no payment detected a Pay on the merchant's or the provider's documents goes as well (no press budget)."""
    from tests.tools.test_browser_payment_surface import TREE

    for caption in ("Confirm", "Pay 100.00 EUR"):
        sup = _go_pay_then([_row(caption)], (BANK_TOP, TREE[1]) if top_case else (TREE[0], BANK_FRAME))
        out, total_reads, pressed = _again(sup, owner_go=True)
        assert out["success"] is True and out["pay_pressed"] is True and pressed == 1 and total_reads == 0, caption
        for tree in (TREE, (TREE[0], PROVIDER_FRAME), (PROVIDER_TOP, TREE[1])):
            sup.tree = sup.listed = tree
            out, _reads, pressed = _again(sup, owner_go=True)
            assert out["success"] is True and pressed == 1, (caption, tree[0][2])
        assert surface.presses_in_lock(sup) == 5
SIBLING_TOP = ("PAGE", "LB", "https://3ds.psp.test/challenge", "https://3ds.psp.test", "")
SIBLING_FRAME = ("CARD", "L9", "https://3ds.psp.test/challenge", "https://3ds.psp.test", "PAGE")
REGISTRY_SITE_TOP = ("PAGE", "LB", "https://hooks.stripe.com/3ds", "https://hooks.stripe.com", "")


@pytest.mark.parametrize("where", ["sibling_top", "sibling_frame", "registry_site_top"])
def test_after_pay_another_host_on_a_providers_site_is_a_bank_step(where):
    """Owner direction 2026-11-18 ("make it all work"): only the exact recorded provider origin is refused after Pay
    (see the provider_top / provider_frame cases); a provider's own 3-D Secure host on the same site as its card
    frames (hooks.stripe.com beside js.stripe.com) is the bank step and is pressed once, with no total read."""
    from tests.tools.test_browser_payment_surface import TREE

    tree = {"sibling_top": (SIBLING_TOP, TREE[1]), "sibling_frame": (TREE[0], SIBLING_FRAME),
            "registry_site_top": (REGISTRY_SITE_TOP, TREE[1])}[where]
    sup = _pay_then([_row("Confirm")], tree)
    out, total_reads, pressed = _again(sup)
    assert out["success"] is True and pressed == 1 and total_reads == 0
    assert surface.presses_in_lock(sup) == 2


def test_a_confirmation_dialog_after_pay_is_pressed_like_any_step():
    """G1 (replaces build C r4's one follow-up press in a new dialog): Place order, then "Confirm purchase" in the
    dialog it opened, both go; with no payment detected nothing limits a further press."""
    from tests.tools.test_browser_payment_surface import APPROVED

    sup = _list_top(_RowsSup([_row("Place order")]))
    assert surface.claim_payment_surface(sup, _hosted()) is None  # the card in the provider's frame only
    surface.record_approved_total(sup, *APPROVED)
    for caption in ("Place order", "Confirm purchase", "Confirm purchase"):
        sup.rows = [_row(caption)]
        out, _reads, pressed = _again(sup, 1)  # the merchant's top document's control (after the card frame's)
        assert out["success"] is True and out["pay_pressed"] is True and pressed == 1, caption
    assert surface.presses_in_lock(sup) == 3


def test_a_covered_control_in_a_hosted_card_frame_names_no_occluder():
    """r4 (Codex final low 3): the provider's document could encode a card-derived bit in what covers its own control:
    a covered control inside the hosted card frame gets the refusal without a class, and nothing is classified; the
    merchant top document's control still names it."""
    sup = _claimed_effect(_list_top(EffectSup()))
    sup.hit = False
    card = surface.payment_click(sup, _ref(sup, CONTINUE))
    assert card["error_type"] == "payment_control_covered" and "occluder" not in card and not _detail_reads(sup)
    assert "dialog" not in card["error"]
    top = surface.payment_click(sup, _ref(sup, len(ROWS_PREP) + CONTINUE))
    assert top["occluder"] == "dialog"
