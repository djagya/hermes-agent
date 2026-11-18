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


def test_a_refill_while_the_old_lock_still_holds_is_unknown_never_zero():
    sup = HolderSup()
    assert surface.claim_payment_surface(sup, _inspected()) is None
    surface.record_approved_total(sup, *APPROVED)
    _press(sup)
    assert surface.claim_payment_surface(sup, _inspected()) is None  # the same tab, still in control's lock
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


class EffectSup(FakeSup):
    """Answers the structural reads: the control's state, the overlay count, the top document; ``after`` (applied at
    the press release) changes them."""

    def __init__(self):
        super().__init__()
        self.state = {"listed": True, "hit": True, "checked": False}
        self.overlays, self.loader, self.occluder = 0, "LP", "dialog"
        self.after = {}

    def default(self, method, params, session):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
            for key, value in self.after.items():
                setattr(self, key, value)
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


def _claimed_effect(sup):
    assert surface.claim_payment_surface(sup, _inspected()) is None
    surface.record_approved_total(sup, *APPROVED)
    return sup


def _ref(sup, index=0):
    return surface.payment_controls(sup)["controls"][index]["ref"]


def test_a_click_that_changed_nothing_says_unchanged():
    sup = _claimed_effect(EffectSup())
    out = surface.payment_click(sup, _ref(sup))
    assert out["success"] is True
    assert out["effect"] == {"navigated": False, "control_listed": True, "control_hit": True, "overlays_before": 0,
                             "overlays_after": 0, "card_frames_before": 1, "card_frames_after": 1,
                             "unchanged": True}


@pytest.mark.parametrize("after, field, value", [
    ({"loader": "LP2"}, "navigated", True),
    ({"overlays": 1}, "overlays_after", 1),
    ({"state": {"listed": False, "hit": False, "checked": False}}, "control_listed", False),
    ({"state": {"listed": True, "hit": False, "checked": False}}, "control_hit", False),
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


def test_the_settle_is_bounded(monkeypatch):
    monkeypatch.setattr(surface, "_SETTLE_S", 0.3)
    sup = _claimed_effect(EffectSup())
    loop = asyncio.new_event_loop()
    try:
        start = loop.time()
        before = {"top": ("PAGE", "LP"), "listed": True, "hit": True, "checked": False, "overlays": 0,
                  "card_frames": 1}
        effect = loop.run_until_complete(surface._effect(sup, "S-page", "el", "S-card", before, "button"))
        assert loop.time() - start < 1.0 and effect["unchanged"] is True
    finally:
        loop.close()


@pytest.mark.parametrize("cls, expected", [("dialog", "dialog"), ("fixed_overlay", "fixed_overlay"),
                                           ("other", "other"), ("Cookie banner: accept all", "other")])
def test_a_covered_control_names_only_the_occluder_class(cls, expected):
    sup = _claimed_effect(EffectSup())
    sup.hit, sup.occluder = False, cls
    out = surface.payment_click(sup, _ref(sup))
    assert out["error_type"] == "payment_control_covered" and out["occluder"] == expected
    assert set(out) == {"success", "error_type", "error", "occluder"} and "Cookie" not in out["error"]
    assert not [c for c in sup.calls if c[0] == "Input.dispatchMouseEvent" and c[1].get("type") == "mousePressed"]


def test_an_overlay_over_the_card_iframe_is_classified_on_the_page():
    sup = _claimed_effect(EffectSup())
    sup.at_point = 99  # another page element sits over the card iframe
    real = sup.on

    def on(method, params, session):
        if method == "Runtime.evaluate" and str(params.get("expression", "")).startswith(surface._OCCLUDER_AT):
            return {"result": {"value": "fixed_overlay"}}
        return real(method, params, session)

    sup.on = on
    out = surface.payment_click(sup, _ref(sup))
    assert out["error_type"] == "payment_control_covered" and out["occluder"] == "fixed_overlay"


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


def test_a_supervisor_without_a_claim_on_a_foreign_lock_is_lost_not_none():
    sup = EffectSup()  # a replaced supervisor or a restarted gateway: no claim of this process
    sup.status = {"locked": True, "native": False, "tabs": ["PAGE"], "unverified": False}
    assert surface.payment_controls(sup)["error_type"] == surface.NATIVE_LOST
    sup.status = dict(UNLOCKED)
    assert surface.payment_controls(sup)["error_type"] == "payment_surface_none"


def test_the_supervisor_does_not_reattach_a_lost_native_socket():
    from tools.browser_supervisor import CDPSupervisor

    sup = _claimed_effect(EffectSup())
    sup.task_id = "t"
    sup._ws = object()
    sup.status = {"locked": True, "native": False, "tabs": ["PAGE"], "unverified": False}
    run = asyncio.run_coroutine_threadsafe(CDPSupervisor._payment_native_lost(sup), sup._loop)
    assert run.result(timeout=10) is True
    assert surface.native_lost_refusal(sup)["error_type"] == surface.NATIVE_LOST
    for status in (dict(UNLOCKED), {"locked": True, "native": True, "tabs": ["PAGE"], "unverified": False},
                   RuntimeError("no answer")):
        sup.status = status
        run = asyncio.run_coroutine_threadsafe(CDPSupervisor._payment_native_lost(sup), sup._loop)
        assert run.result(timeout=10) is False, status
    fresh = FakeSup()  # no lock record: never a status read on redial
    fresh.task_id = "t"
    assert asyncio.run_coroutine_threadsafe(CDPSupervisor._payment_native_lost(fresh), fresh._loop).result(10) is False
    assert not fresh.sent("Hermes.paymentSurfaceStatus")


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

