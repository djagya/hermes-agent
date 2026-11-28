"""D-1209 a (owner 2026-11-27): a second press on the same Pay control is decided by Hermes. Pressing it again is
refused (``payment_same_control``, nothing pressed) unless payment_done answered ``pay_again: true`` after the latest
Pay-like press. The whole input: every state Sera can see after a Pay press, each fault at each read payment_done
makes for ``pay_again``, and the press after it. Fake supervisor and fake CDP only; synthetic data."""

import asyncio
import random
import threading

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
        self.stage = None  # the last again-look read seen (``pressable``, then ``tabs``): where a fault lands
        self.ordered = False  # ``change`` happened: the order shows from then on, whatever a press makes the page show

    def _where(self, method, params):
        expression = params.get("expression")
        fn = params.get("functionDeclaration")
        return {"Target.getTargets": method == "Target.getTargets",
                "isolated_world": method == "Page.createIsolatedWorld",
                "wording_scan": expression == evidence.SCAN_JS,
                "flags": expression == again.FLAGS_JS,
                "tabs": method == "Target.getTargets" and self.stage == "pressable",  # the look's tab read
                "resolve_node": method == "DOM.resolveNode",
                "pressable": fn == again._PRESSABLE}

    async def _cdp(self, method, params=None, *, session_id=None, timeout=10.0):
        where = self._where(method, params or {})
        self.stage = ("pressable" if where["pressable"] else "tabs" if where["tabs"]
                      else None if where["flags"] else self.stage)
        if self.armed and self.fault and where.get(self.fault[0]):
            self.fired += 1
            kind = self.fault[1]
            if kind == "fail":
                raise RuntimeError(f"CDP error on id=1: {method} failed")
            if kind == "slow":  # longer than the read's bound: the look times out, its answer is late
                await asyncio.sleep(self.slow_s)  # health: allow HX013 -- the injected fault is the slowness itself
            if kind == "state":
                self.change()
        return await super()._cdp(method, params, session_id=session_id, timeout=timeout)

    async def _payment_tree(self, sid, child_sessions):
        if self.armed and self.fault == ("tree_after", "state") and self.stage == "tabs":  # the look's last read
            self.fired += 1
            self.change()
        return self.tree

    def change(self):
        """A state change at the faulted read: another Pay-like press on the latest pressed control lands (a
        concurrent payment_click) and the page shows the order in place."""
        with surface._LOCK:
            gen = surface._GENS.get(self)
            if gen.again is not None and gen.again.keys:
                gen.pay_presses += 1
                last = gen.again.keys[-1]
                again.book(gen, last[:3], last[3], {"tabs": gen.again.tabs})
        self.ordered = True
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
        out = super().answer(method, params, session_id)
        if self.ordered and THANKS["text"] not in self.page["text"]:
            self.page["text"] = THANKS["text"] + " " + self.page["text"]
        return out


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


def test_an_unread_node_is_the_same_control_and_never_gets_pay_again():
    """The pressed element's node id could not be read: payment_done cannot check the element (no pay_again), and
    every later Pay-like press on that document is the same control (held), whatever its caption (fails closed)."""
    sup = _sunday()
    real = sup.answer
    sup.answer = lambda m, p, s=None: {} if m == "DOM.describeNode" else real(m, p, s)
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is False
    assert _pay(sup)["error_type"] == SAME
    assert _press(sup, "BESTÄTIGEN")["error_type"] == SAME and len(_mouse_presses(sup)) == 1


RETRY = "JETZT KAUFEN (erneut)"


@pytest.mark.parametrize("unread", ["first", "second"])
@pytest.mark.parametrize("ask", [False, True], ids=["no payment_done", "payment_done"])
def test_a_renamed_control_with_an_unread_id_on_either_press_is_held(unread, ask):
    """Final review #1 (Kimi): the shop renames the pressed JETZT KAUFEN after a decline (the same element, 4001), and
    the element id of one of the two presses was not read: the same document and an unread id is the same control,
    held without pay_again, and with it too when the id at the press is unread (its element cannot be checked)."""
    sup = _sunday()
    real = sup.answer

    def unread_id(m, p, s=None):
        return {} if m == "DOM.describeNode" else real(m, p, s)

    sup.answer = unread_id if unread == "first" else real
    assert _pay(sup, after={"page": DECLINED, "rows": [_row("BESTÄTIGEN"), _row(RETRY, i=1)]})["pay_pressed"] is True
    if ask:
        assert done.payment_done(sup)["pay_again"] is (unread == "second")
    sup.answer = unread_id if unread == "second" else real
    presses = len(_mouse_presses(sup))
    out = _press(sup, RETRY)
    assert out["error_type"] == SAME and len(_mouse_presses(sup)) == presses, out


# ------------------------------------------------------------------ final review #1: what the page says


def _with(text, **after):
    return {"page": {**CART, "text": f"{CART['text']} Noch nicht registriert? {text}"}, **after}


@pytest.mark.parametrize("text, granted", [(t, True) for t in detect_tests.DECLINE_PHRASES]
                         + [(t, False) for t in detect_tests.NOT_DECLINE_PHRASES + detect_tests.PENDING_PHRASES])
def test_only_a_payment_decline_lets_the_same_pay_go_again(text, granted):
    """The permission is its own narrow decline wording, not the wide failure veto: a bare "abgelehnt" or
    "fehlgeschlagen", another feature's error, and a payment still pending (a bank app to approve, a wait) give no
    pay_again, and the same JETZT KAUFEN is held; a real decline (EN, DE) gives one press."""
    sup = _sunday()
    assert _pay(sup, after=_with(text))["pay_pressed"] is True
    out = done.payment_done(sup)
    assert out["pay_again"] is granted, out
    assert out["error"].endswith(done._NOT_REACHED_AGAIN) is granted  # never "ask the user" beside pay_again true
    presses = len(_mouse_presses(sup))
    out = _pay(sup)
    assert (out.get("pay_pressed") is True, len(_mouse_presses(sup)) - presses) == (granted, int(granted)), out
    assert granted or out["error_type"] == SAME


@pytest.mark.parametrize("text", detect_tests.PENDING_PHRASES)
def test_pending_wording_beside_a_flagged_field_gives_no_pay_again(text):
    sup = _sunday()
    assert _pay(sup, after=_with(text, invalid=True))["pay_pressed"] is True
    assert done.payment_done(sup)["pay_again"] is False
    assert _pay(sup)["error_type"] == SAME


# ------------------------------------------------------------------ final review #1: two payment_done calls at once


@pytest.mark.parametrize("answers, ends, pressed", [
    (("yes", "no"), "BA", False),  # Kimi's probe: the older call ends last with its yes
    (("yes", "no"), "AB", False),
    (("no", "yes"), "AB", True),  # the newer call's yes, ending last, stands
    (("no", "yes"), "BA", False),  # an older no ending last clears it (fails closed)
], ids=["older-yes-ends-last", "newer-no-ends-last", "newer-yes-ends-last", "older-no-ends-last"])
def test_overlapping_payment_done_calls_never_store_an_older_yes(monkeypatch, answers, ends, pressed):
    """A started first, B second (each its own thread); each holds at its not-reached exit until released in the
    order ``ends``. A yes is stored only from the payment_done started last; the same JETZT KAUFEN goes only then."""
    sup = _sunday()
    assert _pay(sup, after={"page": DECLINED})["pay_pressed"] is True
    real = again.grant
    at = {name: threading.Event() for name in "AB"}
    go = {name: threading.Event() for name in "AB"}
    said = {}

    def grant(s, token, *call):
        name = threading.current_thread().name
        at[name].set()
        assert go[name].wait(5)
        said[name] = real(s, token, *call)
        return said[name]

    monkeypatch.setattr(again, "grant", grant)
    threads = {name: threading.Thread(target=done.payment_done, args=(sup,), name=name) for name in "AB"}
    for name, answer in zip("AB", answers):
        sup.busy = answer == "no"  # a busy page gives no pay_again
        threads[name].start()
        assert at[name].wait(5)
    sup.busy = False
    for name in ends:
        go[name].set()
        threads[name].join(5)
    assert said["A"] is False and said["B"] is (answers[1] == "yes")
    presses = len(_mouse_presses(sup))
    out = _pay(sup)
    assert (out.get("pay_pressed") is True, len(_mouse_presses(sup)) - presses) == (pressed, int(pressed)), out


# ------------------------------------------------------------------ r2: the evidence first, the grant checked at the press


def test_a_slow_flags_read_never_hides_an_order_shown_before_the_next_press(monkeypatch):
    """The order confirmed in place after the answer (the pressed control gone); the thank-you's Jetzt kaufen is
    pressed with a page-flags read slower than the look's budget: payment_already_made, nothing pressed."""
    monkeypatch.setattr(detect, "_BEFORE_SCAN_S", 1.0)  # stands for the 6 s budget; the flags read takes 1.3 s
    sup = _sunday()
    assert _pay(sup)["pay_pressed"] is True  # nothing visible yet: the order on its way
    sup.page.update(THANKS)
    sup.gone, sup.rows = True, [_row("Weiter einkaufen", role="link"), _row("Weiter", role="link", i=1),
                                _row("Jetzt kaufen", role="link", i=2)]  # another element than the pressed one
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


# ------------------------------------------------------------------ the generator: sequences against a model of the rule

_FAILURE = " Zahlung fehlgeschlagen. Bitte versuche es erneut."
_PENDING = " Bitte warten."
_ORDER = {"weak": " Your payment is processed securely.", "strong": " Vielen Dank für Ihre Bestellung! Bestellnummer 4711."}
AGB = "AGB akzeptieren"


class Rule:
    """The rule in words (D-1209 a, D-1237 a, D-0961), written apart from the code: a Pay-like press on a control
    pressed before in this approval goes only when the last payment_done after the latest Pay-like press said
    pay_again for that control and the page at the press still allows it. payment_done says pay_again only when the
    latest press's answer came back, the page showed neither failure wording nor a flagged field right before it,
    and now shows one of them, with no order wording, nothing busy, the control enabled and no tab a payment tab
    opened, nothing pending. At the press a failure-wording answer needs the wording still there; a flag-only one
    does not. A confirm accepted after a Pay press spends a pay_again."""

    def __init__(self):
        self.page = {"failure": False, "invalid": False, "busy": False, "order": None, "pressable": True,
                     "popup": False, "pending": False}
        self.pressed, self.latest, self.answered, self.before, self.grant = set(), None, False, None, None

    def _allows(self, wording):
        p = self.page
        if p["order"] or p["busy"] or not p["pressable"] or p["popup"] or p["pending"]:
            return False
        return p["failure"] or (p["invalid"] if wording is None else not wording)

    def press(self, caption, effect, uncertain):
        if caption in self.pressed and not (self.grant is not None and self.latest == caption
                                            and self._allows(self.grant)):
            return SAME
        self.before = (self.page["failure"], self.page["invalid"])
        self.pressed.add(caption)
        self.latest, self.answered, self.grant = caption, not uncertain, None
        self.page.update(effect)
        return "uncertain" if uncertain else "pressed"

    def done(self):
        ok = self.latest is not None and self.answered and self.before == (False, False) and self._allows(None)
        self.grant = self.page["failure"] if ok else None
        return ok

    def accept(self):
        self.grant = None
        return True

    def step(self, op):
        """What the rule says for ``op`` (None for a page change or a fix)."""
        if op[0] == "pay":
            return self.press(*op[1:])
        if op[0] == "done":
            return self.done()
        if op[0] == "accept":
            return self.accept()
        self.page.update(op[1] if op[0] == "page" else {"invalid": False})
        return None


def _shown(page):
    """The fake page for the rule's page."""
    text = (CART["text"] + (_FAILURE if page["failure"] else "") + _ORDER.get(page["order"], "")
            + (_PENDING if page["pending"] else ""))
    return {"page": {**CART, "text": text}, **{k: page[k] for k in ("invalid", "busy", "pressable", "popup")}}


def _show(sup, page):
    for key, value in _shown(page).items():
        sup.page.update(value) if key == "page" else setattr(sup, key, value)


def _step(sup, rule, op):
    """``(what the code did, what the rule says)`` for one step."""
    if op[0] == "pay":
        _, caption, effect, uncertain = op
        sup.lose_release = uncertain
        out = _press(sup, caption, after=_shown({**rule.page, **effect}))
        sup.lose_release = False
        got = "pressed" if out.get("pay_pressed") is True else (
            "uncertain" if out.get("error_type") == "payment_click_uncertain" else out.get("error_type"))
        return got, rule.press(caption, effect, uncertain)
    if op[0] == "done":
        out = done.payment_done(sup)
        assert out["error_type"] == "payment_confirmation_not_reached", out
        return out["pay_again"], rule.done()
    if op[0] == "accept":  # the shop's own confirm ("Bestellung abschicken?") after the Pay press, accepted
        sup.dialogs = [done_tests._confirm("Bestellung für 22,66 EUR abschicken?", url=SHOP + "/checkout/")]
        return done.payment_dialog(sup, "d-1", True).get("answered") == "accept", rule.accept()
    rule.page.update(op[1] if op[0] == "page" else {"invalid": False})
    if op[0] == "page":
        _show(sup, rule.page)
        return None, None
    return _press(sup, AGB, after=_shown(rule.page)).get("success"), True  # a fill or preparatory press: no Pay


def _start():
    sup = _sunday()
    sup.rows = list(detect_tests.SUNDAY_ROWS) + [_row(AGB, role="checkbox", i=2)]
    return sup, Rule()


EFFECTS = [{"failure": True}, {"invalid": True}, {"busy": True}, {}, {"failure": True, "busy": True},
           {"failure": True, "invalid": True}, {"order": "weak", "failure": True}, {"order": "strong", "failure": True},
           {"failure": True, "pressable": False}, {"failure": True, "popup": True}, {"failure": True, "pending": True}]
CHANGES = [{"busy": True}, {"busy": False}, {"failure": False}, {"failure": True}, {"order": "weak"},
           {"order": "strong"}, {"order": None}, {"popup": True}, {"pressable": False}, {"pressable": True},
           {"invalid": True}, {"failure": False, "invalid": True}, {"pending": True}, {"pending": False}]
JK = "JETZT KAUFEN"
NAMED = {  # the cases a reviewer found (r3 middle review H1, approach review M1) and the owner's D-0961 flag case
    "false_after_true": (("pay", JK, {"failure": True}, False), ("done",), ("page", {"busy": True}), ("done",),
                         ("page", {"busy": False}), ("pay", JK, {}, False)),
    "busy_at_the_press": (("pay", JK, {"failure": True}, False), ("done",), ("page", {"busy": True}),
                          ("pay", JK, {}, False)),
    "wording_gone_flag_shown": (("pay", JK, {"failure": True}, False), ("done",),
                                ("page", {"failure": False, "invalid": True}), ("pay", JK, {}, False)),
    "flag_fixed": (("pay", JK, {"invalid": True}, False), ("done",), ("fix",), ("pay", JK, {"failure": True}, False),
                   ("done",), ("pay", JK, {}, False)),
    "two_controls": (("pay", "BESTÄTIGEN", {}, False), ("pay", JK, {"failure": True}, False), ("done",),
                     ("pay", "BESTÄTIGEN", {}, False), ("pay", JK, {}, False)),
    "popup_between_grant_and_press": (("pay", JK, {"failure": True}, False), ("done",), ("page", {"popup": True}),
                                      ("pay", JK, {}, False)),
    "popup_before_done": (("pay", JK, {"failure": True}, False), ("page", {"popup": True}), ("done",)),
    # final review #1 (Kimi): an accepted confirm may place the order; a payment_done after it answers again
    "dialog_accept": (("pay", JK, {"failure": True}, False), ("done",), ("accept",), ("pay", JK, {}, False)),
    "dialog_accept_then_done": (("pay", JK, {"failure": True}, False), ("done",), ("accept",), ("done",),
                                ("pay", JK, {}, False)),
    "pending_at_the_press": (("pay", JK, {"failure": True}, False), ("done",), ("page", {"pending": True}),
                             ("pay", JK, {}, False)),
    "flag_with_pending": (("pay", JK, {"invalid": True, "pending": True}, False), ("done",), ("pay", JK, {}, False)),
}


def _generated(n=48, length=6):
    rng = random.Random(1209)
    effects = EFFECTS + [{"failure": True}] * 4 + [{"invalid": True}] * 2  # the ones that can lead to pay_again
    out = []
    for _ in range(n):
        ops = []
        while len(ops) < length:
            r = rng.random()
            if r < 0.4:
                ops.append(("pay", JK if r < 0.3 else "BESTÄTIGEN", rng.choice(effects), rng.random() < 0.08))
                if rng.random() < 0.6:
                    ops.append(("done",))
                if rng.random() < 0.4:  # the same control right after its answer
                    ops.append(("pay", ops[-2 if ops[-1] == ("done",) else -1][1], rng.choice(effects), False))
            elif r < 0.65:
                ops.append(("done",))
            elif r < 0.9:
                ops.append(("page", rng.choice(CHANGES)))
            else:
                ops.append(("fix",))
        out.append(tuple(ops))
    return out


GENERATED = _generated()
SEQUENCES = list(NAMED.values()) + GENERATED


@pytest.fixture
def _short(monkeypatch):
    monkeypatch.setattr(done, "_WAIT_S", 0.12)
    monkeypatch.setattr(detect, "_BEFORE_SCAN_S", 0.5)
    monkeypatch.setattr(detect, "_FLAGS_S", 0.3)


@pytest.mark.parametrize("name", [*NAMED, "generated"])
def test_every_sequence_does_what_the_rule_says(_short, name):
    """Done when 1-3: each step of each sequence (Pay presses on two controls with what the page shows after them,
    payment_done, page changes between calls, a field fixed by a non-Pay press) against the rule."""
    seen = {"granted": 0, "again": 0, "held": 0}
    for ops in GENERATED if name == "generated" else [NAMED[name]]:
        sup, rule = _start()
        for i, op in enumerate(ops):
            again_press = op[0] == "pay" and op[1] in rule.pressed
            got, want = _step(sup, rule, op)
            assert got == want, (ops, i, got, want)
            seen["granted"] += got is True and op[0] == "done"
            seen["again"] += again_press and got == "pressed"
            seen["held"] += got == SAME
    assert name != "generated" or min(seen.values()) >= 5, seen  # the generator reaches each outcome


def _sites():
    """Per fault side, the prefixes whose last step is permissive: ``done`` (pay_again true), ``press`` (the same
    control pressed again), ``base`` (a first Pay press whose payment_done right after it says pay_again)."""
    sites = {"done": [], "press": [], "base": []}
    for ops in SEQUENCES:
        rule, outcomes = Rule(), []
        for i, op in enumerate(ops):
            again_press = op[0] == "pay" and op[1] in rule.pressed
            want = rule.step(op)
            outcomes.append((op, want, again_press))
            if op[0] == "done" and want is True:
                sites["done"].append((ops[:i + 1], i))
                prev_op, prev_want, prev_again = outcomes[i - 1]
                if prev_op[0] == "pay" and prev_want == "pressed" and not prev_again:
                    sites["base"].append((ops[:i + 1], i - 1))
            if again_press and want == "pressed":
                sites["press"].append((ops[:i + 1], i))
    return sites


KINDS = ("fail", "slow", "state")
FAULT_RUNS = ([("done", w, k) for w in ("Target.getTargets", "isolated_world", "wording_scan", "flags", "resolve_node",
                                        "pressable", "tabs") for k in KINDS] + [("done", "tree_after", "state")]
              + [("press", w, k) for w in ("wording_scan", "flags", "resolve_node", "pressable", "tabs") for k in KINDS]
              + [("base", w, k) for w in ("wording_scan", "flags", "tabs") for k in KINDS])
SITES = _sites()


@pytest.mark.parametrize("n, run", list(enumerate(FAULT_RUNS)), ids=[f"{s}-{w}-{k}" for s, w, k in FAULT_RUNS])
def test_a_fault_at_any_read_takes_the_permission_away(_short, n, run):
    """Each read on both sides failed, slower than its bound (its answer late) or with the state changed at it
    (another press lands, the order shows), on a step the rule (and the run without the fault, checked above) makes
    permissive: the step, or the payment_done right after a faulted first press, is no longer permissive, and (no
    pay_again stored) the same control is then held."""
    side, where, kind = run
    ops, at = SITES[side][n % len(SITES[side])]
    sup, rule = _start()
    sup.slow_s = 1.1
    for i, op in enumerate(ops):
        sup.fault, sup.armed, sup.stage = (where, kind), i == at, None
        got, want = _step(sup, rule, op)
        sup.armed = False
        if i == at:
            assert sup.fired > 0, "the fault was injected"
        if i < at or (side == "base" and i == at):
            assert got == want, (ops, i, got, want)
    assert got in (False, SAME), (ops, got)  # the clean run said True or pressed here
    if side != "press":  # no pay_again stored: the same control is held (a refused press leaves the stored one)
        presses = len(_mouse_presses(sup))
        assert _press(sup, rule.latest)["error_type"] == SAME and len(_mouse_presses(sup)) == presses
