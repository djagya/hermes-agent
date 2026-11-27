"""D-1209 a (owner 2026-11-27): a second press on the same Pay control is decided by Hermes. Pressing it again is
refused (``payment_same_control``, nothing pressed) unless payment_done answered ``pay_again: true`` after the latest
Pay-like press. The whole input: every state Sera can see after a Pay press, each fault at each read payment_done
makes for ``pay_again``, and the press after it. Fake supervisor and fake CDP only; synthetic data."""

import asyncio

import pytest

from tests.tools import test_browser_payment_detect as detect_tests
from tests.tools import test_browser_payment_done as done_tests
from tests.tools.test_browser_payment_surface_honesty import _mouse_presses, _row
from tools import browser_payment_again as again
from tools import browser_payment_detect as detect
from tools import browser_payment_done as done
from tools import browser_payment_order_evidence as evidence
from tools import browser_payment_surface as surface

SHOP = done_tests.SHOP
CART, DECLINED, THANKS = detect_tests.CART, detect_tests.DECLINED, detect_tests.THANKS
NEW_DOC = detect_tests.NEW_DOC
SAME = again.SAME_CONTROL
PICK = "function(i){return this[i]}"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(surface, "_managed", lambda: False)
    monkeypatch.setattr(surface, "_SETTLE_S", 0.02)
    monkeypatch.setattr(surface, "_SETTLE_POLL_S", 0.01)
    monkeypatch.setattr(done, "_POLL_S", 0.0)
    monkeypatch.setattr(done, "_WAIT_S", 0.4)
    monkeypatch.setattr(done, "_RELEASE_WAIT_S", 0.2)
    monkeypatch.setattr(done, "_RELEASE_POLL_S", 0.01)


class AgainSup(detect_tests.ShopSup):
    """The Sunday checkout where each listed control is its own element (backendNodeId 4000 + its index), whose
    pressed element answers ``pressable`` (connected, rendered, enabled) and whose page answers ``invalid`` / ``busy``.
    ``fault``: ``(where, kind)`` armed once the first Pay press is answered (``armed``; ``arm_at_release``: when a
    press is released); ``fired`` counts its hits; a slow call takes ``slow_s``."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pressable, self.invalid, self.busy, self.gone = True, False, False, False
        self.lose_release, self.popup = False, False
        self.fault, self.armed, self.fired, self.arm_at_release, self.slow_s = None, False, 0, False, 1.3

    def _where(self, method, params):
        expression = params.get("expression")
        fn = params.get("functionDeclaration")
        return {"Target.getTargets": method == "Target.getTargets",
                "isolated_world": method == "Page.createIsolatedWorld",
                "wording_scan": expression == evidence.SCAN_JS,
                "flags": expression == again.FLAGS_JS,
                "tabs_check": method == "Target.getTargets" and bool(self.calls)
                and self.calls[-1][1].get("expression") == again.FLAGS_JS,
                "resolve_node": method == "DOM.resolveNode",
                "pressable": fn == again._PRESSABLE}

    async def _cdp(self, method, params=None, *, session_id=None, timeout=10.0):
        if self.armed and self.fault and self._where(method, params or {}).get(self.fault[0]):
            self.fired += 1
            kind = self.fault[1]
            if kind == "fail":
                raise RuntimeError(f"CDP error on id=1: {method} failed")
            if kind == "slow":  # longer than payment_done's probe bound: the look times out, its answer is late
                await asyncio.sleep(self.slow_s)  # health: allow HX013 -- the injected fault is the slowness itself
            if kind == "state":
                self.change()
        return await super()._cdp(method, params, session_id=session_id, timeout=timeout)

    async def _payment_tree(self, sid, child_sessions):
        if self.armed and self.fault == ("tree_after", "state") and self.calls and self.calls[-1][1].get(
                "functionDeclaration") == again._PRESSABLE:
            self.fired += 1
            self.change()
        return self.tree

    def change(self):
        """A state change at the faulted read: a Pay-like press lands (another payment_click) and the page shows
        the order."""
        with surface._LOCK:
            gen = surface._GENS.get(self)
            gen.pay_presses += 1
        self.page.update(THANKS)

    def answer(self, method, params, session_id=None):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased" and self.arm_at_release:
            self.armed = True
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased" and self.lose_release:
            self.lose_release = False
            super().answer(method, params, session_id)  # the press landed; its reply is lost
            raise TimeoutError("lost reply")
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == PICK:
            return {"result": {"objectId": f"el{params['arguments'][0]['value']}"}}
        if method == "DOM.describeNode" and str(params.get("objectId", "")).startswith("el"):
            return {"node": {"backendNodeId": 4000 + int(params["objectId"][2:])}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"pay-{params['backendNodeId']}"}}
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == evidence._SHOWN:
            return {"result": {"value": not self.gone}}
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == again._PRESSABLE:
            return {"result": {"value": self.pressable and not self.gone}}
        if method == "Runtime.evaluate" and params.get("expression") == again.FLAGS_JS:
            return {"result": {"type": "object", "value": {"invalid": self.invalid, "busy": self.busy}}}
        if method == "Target.getTargets" and self.popup:
            infos = super().answer(method, params, session_id)
            return {"targetInfos": infos["targetInfos"] + [
                {"targetId": "POP", "type": "page", "openerId": "TAB", "url": "https://bank.test/redirect"}]}
        return super().answer(method, params, session_id)


def _sunday():
    sup = done_tests._claimed(AgainSup([done_tests.CHECKOUT], status=dict(done_tests.LOCKED_TAB)))
    surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), SHOP)
    sup.page.update(CART)
    return sup


_press = detect_tests._press


def _pay(sup, after=None):
    return _press(sup, "JETZT KAUFEN", after=after)


# ------------------------------------------------------------------ what Sera can see after a Pay press


def _declined(sup):
    sup.after = {"page": DECLINED}  # the shop says the payment failed, its JETZT KAUFEN still there


def _flagged(sup):
    sup.after = {"invalid": True}  # a field the form needs is flagged after the press (aria-invalid)


def _slow_in_place(sup):
    sup.after = {"busy": True}  # an XHR order still running: a spinner, the Pay control kept


def _declined_while_busy(sup):
    sup.after = {"page": DECLINED, "busy": True}  # failure wording while the page is still busy


def _quiet(sup):
    pass  # the page shows nothing new: the order may be on its way (XHR), the Pay control kept


def _confirmed_with_failure_words(sup):  # a real confirmation in place that also carries failure-like wording
    sup.after = {"page": {**CART, "text": "Vielen Dank für Ihre Bestellung! Zahlung fehlgeschlagen? Schreib uns."}}


def _order_wording_new(sup):  # order wording that was not there before, beside failure wording
    sup.after = {"page": {**CART, "text": CART["text"] + " Ihre Bestellung ist bei uns eingegangen. "
                                                         "Es ist ein Fehler aufgetreten."}}


def _order_wording_before(sup):  # the checkout carries order wording before the press; failure wording after it
    sup.page.update({**CART, "text": CART["text"] + " Your payment is processed securely."})
    sup.after = {"page": {**CART, "text": CART["text"] + " Your payment is processed securely. Ihre Bestellung ist "
                                                         "bei uns eingegangen. Es ist ein Fehler aufgetreten."}}


def _remounted(sup):  # quiet page; the shop re-renders its buttons: JETZT KAUFEN is a new element, same caption
    sup.after = {"rows": [_row("JETZT KAUFEN")]}


def _failed_before(sup):
    sup.page.update(DECLINED)  # the failure was on the page before this press (an earlier attempt's)


def _control_disabled(sup):
    sup.after = {"page": DECLINED, "pressable": False}


def _uncertain(sup):
    sup.after, sup.lose_release = {"page": DECLINED}, True  # payment_click_uncertain


def _in_flight(sup):
    sup.after = {"page": DECLINED}
    sup.loading = True  # the press started a navigation that has not finished loading


def _popup(sup):
    sup.after = {"page": DECLINED, "popup": True}  # the press opened a tab (a bank's redirect)


def _bank_page(sup):
    sup.after = {"page": DECLINED, "tree": done_tests.ACS}  # the tab went to the bank's 3-D Secure page


def _detected(sup):  # payment_detected: the thank-you in place, the pressed control gone
    sup.after = {"page": THANKS, "gone": True, "rows": [_row("Weiter einkaufen", role="link"),
                                                        _row("Jetzt kaufen", role="link", i=1)]}


# (state, payment_done is called, pay_again, the same control's second press: None = pressed, else its refusal)
TABLE = [
    (_declined, True, True, None),
    (_flagged, True, True, None),
    (_declined, False, None, SAME),  # Sera presses again without asking payment_done
    (_slow_in_place, True, False, SAME),
    (_declined_while_busy, True, False, SAME),
    (_quiet, True, False, SAME),
    (_confirmed_with_failure_words, True, False, SAME),
    (_order_wording_new, True, False, SAME),
    (_order_wording_before, True, False, SAME),
    (_remounted, False, None, SAME),  # a new node id, the same caption on the same document: the same control
    (_failed_before, True, False, SAME),
    (_control_disabled, True, False, SAME),
    (_uncertain, True, False, SAME),
    (_in_flight, True, False, detect.INFLIGHT),
    (_popup, True, False, SAME),
    (_bank_page, True, False, None),  # the bank's page: the old control is not listed; see below
    (_detected, False, None, detect.SPENT),
]


@pytest.mark.parametrize("state, ask, pay_again, second", TABLE, ids=[row[0].__name__ for row in TABLE])
def test_the_same_pay_control_goes_again_only_after_pay_again(state, ask, pay_again, second):
    sup = _sunday()
    state(sup)
    loading = getattr(sup, "loading", False)
    first = _pay(sup, after=sup.after)
    assert first.get("pay_pressed") is True or first["error_type"] == "payment_click_uncertain", first
    if loading:
        sup._on_frame_loading({"frameId": "TOP", "navigationType": "differentDocument"})
    if ask:
        out = done.payment_done(sup)
        assert out["error_type"] == "payment_confirmation_not_reached" and out["pay_again"] is pay_again, out
        assert again.AGAIN_RULE in out["error"]
    if state is _bank_page:  # nothing of the checkout's document is listed; back on it, its Pay is held
        sup.tree = done_tests.CHECKOUT
        second = SAME
    presses = len(_mouse_presses(sup))
    out = _press(sup, "Jetzt kaufen") if state is _detected else _pay(sup)  # the thank-you's recommendation
    if second is None:
        assert out["success"] is True and out["pay_pressed"] is True and len(_mouse_presses(sup)) == presses + 1
        third = _pay(sup)  # pay_again covers one press
        assert third["error_type"] == SAME and len(_mouse_presses(sup)) == presses + 1
    else:
        assert out["error_type"] == second and len(_mouse_presses(sup)) == presses, out
        if second == SAME:
            assert "payment_done" in out["error"]


# ------------------------------------------------------------------ faults at each read payment_done makes


FAULTS = [(where, kind) for where in ("Target.getTargets", "isolated_world", "wording_scan", "flags", "tabs_check",
                                      "resolve_node", "pressable")
          for kind in ("fail", "slow", "state")] + [("tree_after", "state")]


@pytest.mark.parametrize("fault", FAULTS, ids=[f"{w}-{k}" for w, k in FAULTS])
def test_a_failed_slow_or_changed_read_gives_no_pay_again(fault):
    """The pay_again row (a decline in place) with one fault from the first Pay press on: a failed call, a call slower
    than payment_done's probe bound (its answer comes late), or a state change at that read (another Pay press lands,
    the page shows the order). Each one turns the row's pay_again off (none is a no-op), and the same control stays
    refused."""
    sup = _sunday()
    first = _pay(sup, after={"page": DECLINED})
    assert first["pay_pressed"] is True
    sup.fault, sup.armed = fault, True
    out = done.payment_done(sup)
    assert sup.fired > 0, "the fault was injected"
    assert out["error_type"] == "payment_confirmation_not_reached" and out["pay_again"] is False, out
    sup.armed = False
    presses = len(_mouse_presses(sup))
    second = _pay(sup)
    assert second["success"] is False and len(_mouse_presses(sup)) == presses, second


def test_without_a_fault_the_same_row_gives_pay_again():
    """The control for the fault table: the same steps with nothing injected."""
    sup = _sunday()
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    sup.fault, sup.armed = ("flags", "fail"), False
    assert done.payment_done(sup)["pay_again"] is True and sup.fired == 0


def test_a_granted_press_that_comes_back_uncertain_gives_no_further_pay_again():
    sup = _sunday()
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is True
    sup.lose_release = True
    assert _pay(sup)["error_type"] == "payment_click_uncertain"
    assert done.payment_done(sup)["pay_again"] is False
    assert _pay(sup)["error_type"] == SAME and len(_mouse_presses(sup)) == 2


def test_another_pay_like_press_after_pay_again_voids_it():
    sup = _sunday()
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is True
    assert _press(sup, "BESTÄTIGEN")["pay_pressed"] is True  # another Pay-like control: the next step
    assert _pay(sup)["error_type"] == SAME, "pay_again was for the press before this one"


def test_a_preparatory_press_after_pay_again_keeps_it_while_the_failure_shows():
    """A checkbox is no Pay-like press: the same Pay then goes once while the page still shows the failure. Once the
    flag is gone (the field fixed) the page right before the press shows no failure: pay_again no longer counts (r2:
    the grant is checked again at the press; refusing costs one reply to the owner)."""
    sup = _sunday()
    sup.rows = list(detect_tests.SUNDAY_ROWS) + [_row("AGB akzeptieren", role="checkbox", i=2)]
    assert _pay(sup, after={"page": DECLINED, "invalid": True})["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is True
    assert _press(sup, "AGB akzeptieren", after={"invalid": False})["success"] is True
    assert _pay(sup)["pay_pressed"] is True and len(_mouse_presses(sup)) == 3
    sup2 = _sunday()
    sup2.rows = list(sup.rows)
    assert _pay(sup2, after={"invalid": True})["pay_pressed"] is True
    assert done.payment_done(sup2)["pay_again"] is True
    assert _press(sup2, "AGB akzeptieren", after={"invalid": False})["success"] is True
    assert _pay(sup2)["error_type"] == SAME and len(_mouse_presses(sup2)) == 2


# ------------------------------------------------------------------ what the gate never holds


def test_a_two_step_checkout_is_not_held():
    """Done when 3: BESTÄTIGEN then JETZT KAUFEN (another control on the same document), and a Pay on a new document
    after a press, go without payment_done."""
    sup = _sunday()
    assert _press(sup, "BESTÄTIGEN")["pay_pressed"] is True
    assert _pay(sup, after={"tree": NEW_DOC, "page": DECLINED})["pay_pressed"] is True
    assert _pay(sup)["pay_pressed"] is True, "the shop's new document: its JETZT KAUFEN is another control"
    assert _pay(sup)["error_type"] == SAME and len(_mouse_presses(sup)) == 3


def test_a_new_approval_starts_a_new_gate():
    """Done when 4: the owner's next Approve (a new bound total) covers its own Pay press."""
    sup = _sunday()
    assert _pay(sup)["pay_pressed"] is True
    assert _pay(sup)["error_type"] == SAME
    surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), SHOP)
    assert _pay(sup)["pay_pressed"] is True and len(_mouse_presses(sup)) == 2


def test_the_existing_refusals_come_first():
    """Done when 4: a changed total refuses before the gate, as before (nothing pressed either way)."""
    sup = _sunday()
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    sup.pin = {"state": "matched", "labelled": [2999], "stated": 1, "unclear": False}
    assert _pay(sup)["error_type"] == surface.TOTAL_CHANGED and len(_mouse_presses(sup)) == 1


def test_an_unread_node_is_told_by_its_caption_and_never_gets_pay_again():
    """The pressed element's node id could not be read: the same caption on the same document is the same control
    (held), another caption is another control, and payment_done cannot check the element: no pay_again."""
    sup = _sunday()
    real = sup.answer
    sup.answer = lambda m, p, s=None: {} if m == "DOM.describeNode" else real(m, p, s)
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is False
    assert _pay(sup)["error_type"] == SAME
    assert _press(sup, "BESTÄTIGEN")["pay_pressed"] is True


# ------------------------------------------------------------------ r2: the evidence first, the grant checked at the press


def test_a_slow_flags_read_never_hides_an_order_shown_before_the_next_press(monkeypatch):
    """The order confirmed in place after the answer (the pressed control gone); the thank-you's Jetzt kaufen is
    pressed with a page-flags read slower than the look's budget: payment_already_made, nothing pressed."""
    monkeypatch.setattr(detect, "_BEFORE_SCAN_S", 1.0)  # stands for the 6 s budget; the flags read takes 1.3 s
    sup = _sunday()
    assert _pay(sup)["pay_pressed"] is True  # nothing visible yet: the order on its way
    sup.page.update(THANKS)
    sup.gone, sup.rows = True, [_row("Weiter einkaufen", role="link"), _row("Jetzt kaufen", role="link", i=1)]
    sup.fault, sup.armed = ("flags", "slow"), True
    presses = len(_mouse_presses(sup))
    out = _press(sup, "Jetzt kaufen")
    assert out["error_type"] == detect.SPENT and len(_mouse_presses(sup)) == presses, out


def test_a_slow_flags_read_never_delays_the_detection_after_the_press(monkeypatch):
    """No page-flags read after a press: a flags read slower than the answer's window cannot lose the thank-you."""
    monkeypatch.setattr(surface, "_ACTION_TIMEOUT_S", 3.0)
    monkeypatch.setattr(surface, "_PRESS_MARGIN_S", 1.0)
    sup = _sunday()
    sup.fault, sup.arm_at_release, sup.slow_s = ("flags", "slow"), True, 3.0
    out = _pay(sup, after={"page": THANKS, "gone": True, "rows": [_row("Weiter einkaufen", role="link")]})
    assert out["pay_pressed"] is True and out["payment_detected"] is True, out
    with surface._LOCK:
        assert surface._GENS[sup].paid is True


def test_a_slow_flags_read_before_the_press_gives_no_pay_again(monkeypatch):
    """The flags read right before the press is dropped when slower than its own bound: no pay_again later."""
    monkeypatch.setattr(detect, "_FLAGS_S", 0.2, raising=False)
    sup = _sunday()
    sup.fault, sup.armed = ("flags", "slow"), True
    assert _pay(sup, after={"invalid": True, "armed": False})["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is False


def test_an_unread_tab_list_before_the_press_gives_no_pay_again(monkeypatch):
    async def unread(_sup):
        return None

    monkeypatch.setattr(detect, "page_tabs", unread)
    sup = _sunday()
    assert _pay(sup, after={"page": DECLINED, "popup": True})["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is False
    assert _pay(sup)["error_type"] == SAME


def test_a_tab_the_press_opened_late_gives_no_pay_again():
    """The bank's tab opens after the press's answer came back (a slow XHR, then window.open)."""
    sup = _sunday()
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    sup.popup = True
    assert done.payment_done(sup)["pay_again"] is False
    assert _pay(sup)["error_type"] == SAME and len(_mouse_presses(sup)) == 1


def _thanks_in_place(sup):  # the order confirmed in place, the Pay control kept, the failure line still there
    sup.page.update({**CART, "text": "Vielen Dank für Ihre Bestellung! Bestellnummer 12345. Zahlung fehlgeschlagen? "
                                     "Schreib uns."})


def _order_number_in_place(sup):  # failure wording gone, an order number shown, the Pay control kept
    sup.page.update({**CART, "text": CART["text"] + " Vielen Dank für Ihre Bestellung! Bestellnummer 4711."})


def _failure_gone(sup):  # the page shows nothing any more (the order may have gone through by XHR)
    sup.page.update(CART)


def _new_tab(sup):  # a tab opened after payment_done's answer
    sup.popup = True


@pytest.mark.parametrize("change", [_thanks_in_place, _order_number_in_place, _failure_gone, _new_tab],
                         ids=lambda f: f.__name__)
def test_pay_again_counts_only_while_the_page_still_shows_the_failure(change):
    """payment_done granted pay_again; before the press the page changed: the grant does not cover the press."""
    sup = _sunday()
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is True
    change(sup)
    out = _pay(sup)
    assert out["error_type"] in (SAME, detect.SPENT) and len(_mouse_presses(sup)) == 1, out
