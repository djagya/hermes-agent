"""Build F (2026-11-18 Sunday live retry, Unzer UI v1): a "BESTÄTIGEN" that only confirmed the card in the payment-method
modal used the approval's one Pay press, the page's "JETZT KAUFEN" was then refused, and payment_done reported the
checkout itself as the order confirmation. Round 2 (reviews): a confirm is preparatory only on positive evidence (its
box holds the owner <iframe> of a card frame this fill claimed, a purchase button lies outside it), once per approval
and provisionally (re-booked as the Pay press when it navigated or the purchase button went away); payment_done needs
order evidence, rejects failure wording and tells the pressed Pay control by identity. Fake supervisor and fake CDP
only; synthetic data."""

import json
import shutil
import subprocess

import pytest

from tools import browser_payment_done as done
from tools import browser_payment_order_evidence as evidence
from tools import browser_payment_press_kind as kind
from tools import browser_payment_surface as surface
from tests.tools import test_browser_payment_done as done_tests
from tests.tools.test_browser_payment_surface import APPROVED
from tests.tools.test_browser_payment_surface_honesty import DialogSup, EffectSup, _hosted, _mouse_presses, _row


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(surface, "_managed", lambda: False)
    monkeypatch.setattr(surface, "_SETTLE_S", 0.02)
    monkeypatch.setattr(surface, "_SETTLE_POLL_S", 0.01)
    monkeypatch.setattr(done, "_POLL_S", 0.0)
    monkeypatch.setattr(done, "_WAIT_S", 0.4)
    monkeypatch.setattr(done, "_RELEASE_WAIT_S", 0.2)
    monkeypatch.setattr(done, "_RELEASE_POLL_S", 0.01)


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the page script")
    run = subprocess.run([node], input=script, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# ------------------------------------------------------------------ which captions only confirm


@pytest.mark.parametrize("caption, strong, confirm_only", [
    ("BESTÄTIGEN", False, True), ("Bestätigen", False, True), ("Confirm", False, True), ("Submit", False, True),
    ("Confirm payment method", False, True), ("Confirmer le moyen de paiement", False, True),
    ("Auswahl bestätigen", False, True), ("Abschließen", False, True),
    ("Zahlung bestätigen", True, False), ("confirm payment", True, False), ("Bestellung bestätigen", True, False),
    ("confirm order", True, False), ("confirmer le paiement", True, False), ("confirmar pago", True, False),
    ("Kauf abschließen", True, False), ("JETZT KAUFEN", True, False), ("Pay 22.66 EUR", True, False),
    ("Zahlungspflichtig bestellen", True, False), ("Continue", False, False), (surface.CAPTION_WITHHELD, False, False),
    # build F r2 (reviews): reservations, checkouts, registrations, subscriptions, donations and tickets are purchases
    ("Confirm reservation", True, False), ("Complete reservation", True, False), ("Complete checkout", True, False),
    ("Reservierung bestätigen", True, False), ("Confirm subscription", True, False), ("Abo bestätigen", True, False),
    ("Complete registration", True, False), ("Anmeldung abschließen", True, False), ("Confirm donation", True, False),
    ("Confirm tickets", True, False), ("Confirm booking", True, False), ("Buchung bestätigen", True, False),
    ("Confirmar reserva", True, False), ("Confirmer la réservation", True, False),
])
def test_a_confirm_word_names_the_purchase_only_with_a_purchase_noun(caption, strong, confirm_only):
    assert kind.strong_pay(caption) is strong and kind.confirm_only(caption) is confirm_only
    assert surface._pay_press("button", caption) is (caption not in ("Continue",)), "the Pay-like set is unchanged"


@pytest.mark.parametrize("control, purchase", [
    (_row("JETZT KAUFEN"), True), (_row("Place order"), True), (_row("Complete checkout"), True),
    (_row("JETZT KAUFEN", role="link"), False),  # a link (a product tile) is never the checkout's order button
    ({**_row("JETZT KAUFEN"), "disabled": True}, False),
    (_row("Buy with Apple Pay"), False), (_row("Google Pay"), False), (_row("Mit PayPal bezahlen"), False),
    (_row("Amazon Pay"), False), (_row("Klarna Sofort bezahlen"), False), (_row("Pay later"), False),
    (_row("Gutschein kaufen"), False), (_row("Buy a gift card"), False), (_row("Bestätigen"), False),
])
def test_only_an_enabled_non_wallet_purchase_button_counts_as_the_outside_purchase(control, purchase):
    assert kind.purchase_button(control) is purchase


# ------------------------------------------------------------------ the page script (node)

_DOM = r"""
const E = (tag, kids = [], o = {}) => {
  const el = Object.assign({tagName: tag, kids, parentElement: null, isConnected: true}, o);
  for (const k of kids) k.parentElement = el;
  el.contains = x => { for (let n = x; n; n = n.parentElement) if (n === el) return true; return false; };
  return el;
};
const owner = () => E('iframe');  // the owner <iframe> of a card frame this fill claimed (resolved by frame id)
const confirm = E('button'), buy = E('button');
"""
# (body, owners, expected): ``owners`` are the claimed card frames' owner elements passed to the script
_LAYOUTS = {
    # Unzer's method modal: the card frames and "BESTÄTIGEN" in one box, "JETZT KAUFEN" on the page
    "method modal": ("const o1 = owner(), o2 = owner(); const body = E('body', [E('main', [buy]), "
                     "E('div', [E('div', [o1, o2]), E('div', [confirm])])]);", "o1, o2", True),
    "card box in the page": ("const o1 = owner(); const body = E('body', [E('main', [buy]), "
                             "E('section', [o1, E('div', [confirm])])]);", "o1", True),
    "purchase in the same box": ("const o1 = owner(); const body = E('body', [E('div', [o1, confirm, buy])]);",
                                 "o1", False),
    # Codex probe: an order button in a fixed footer, the card form in the page: no card frame in the confirm's box
    "fixed footer order button": ("const o1 = owner(); const body = E('body', [E('main', [buy, E('form', [o1])]), "
                                  "E('div', [confirm], {fixed: true})]);", "o1", False),
    # an iframe that is not a claimed card frame's owner (ads, a src-less frame) never makes a box
    "another frame": ("const o1 = owner(); const body = E('body', [E('main', [buy]), E('section', [E('iframe'), "
                      "confirm]), E('aside', [o1])]);", "o1", False),
    "no owner": ("const body = E('body', [E('main', [buy]), E('section', [E('iframe'), confirm])]);", "", False),
    "owner disconnected": ("const o1 = owner(); o1.isConnected = false; const body = E('body', [E('main', [buy]), "
                           "E('section', [o1, confirm])]);", "o1", False),
    "card owner only at body": ("const o1 = owner(); const body = E('body', [buy, o1, confirm]);", "o1", False),
}


@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_the_confirms_box_is_the_one_holding_a_claimed_card_frames_owner(layout):
    body, owners, expected = _LAYOUTS[layout]
    harness = (_DOM + body + "\nconst html = E('html', [body]);\nconst document = {body, documentElement: html};\n"
               f"process.stdout.write(JSON.stringify(({kind.PREP_CHECK}).call(confirm, [confirm, buy], [1]"
               f"{', ' + owners if owners else ''})));")
    assert _node(harness) is expected


def test_the_owner_taken_is_the_card_frames_ancestor_below_the_top_document():
    tree = (("TOP", "L0", "u", "o", ""), ("WRAP", "L1", "u", "o", "TOP"), ("CARD", "L2", "u", "o", "WRAP"),
            ("CVC", "L3", "u", "o", "TOP"), ("ADS", "L4", "u", "o", "TOP"))
    assert kind._top_children(tree, frozenset({"CARD", "CVC", "GONE"})) == ["WRAP", "CVC"]


# ------------------------------------------------------------------ the Sunday checkout (payment_click)


class SundaySup(DialogSup, EffectSup):
    """The merchant top document lists ``rows`` (card frame first, then the top: indices offset by ``len(rows)``).
    ``prep``: what the box check answers on each read (the last one repeats; an exception: the read fails);
    ``still``: what the purchase buttons' re-read after a preparatory press answers; ``owner``: whether the card
    frame's owner <iframe> resolves into the controls' world; ``node``: the pressed control's backendNodeId."""

    def __init__(self, rows, prep=(True,)):
        super().__init__(rows)
        self.prep, self.prep_calls, self.still, self.owner, self.node = list(prep), [], True, True, None

    started_navigating = False  # Chrome's navigation-start events since the press (build F r3)

    def load_started_since(self, frame_id, since):
        return self.started_navigating

    def default(self, method, params, session):
        fn = params.get("functionDeclaration", "")
        if method == "Runtime.callFunctionOn" and fn == kind.PREP_CHECK:
            self.prep_calls.append(params["arguments"])
            answer = self.prep.pop(0) if len(self.prep) > 1 else self.prep[0]
            if isinstance(answer, Exception):
                raise answer
            return {"result": {"value": answer}}
        if method == "Runtime.callFunctionOn" and fn == kind.WATCH_LEAVE:
            self.watched = getattr(self, "watched", 0) + 1
            return {"result": {"objectId": "watch"} if getattr(self, "watch", True) else {"type": "undefined"}}
        if method == "Runtime.callFunctionOn" and fn == kind.WATCH_READ:
            self.watch_reads = getattr(self, "watch_reads", 0) + 1
            return {"result": {"value": getattr(self, "left", False)}}
        if method == "Runtime.callFunctionOn" and fn == kind.STILL_LISTED:
            if isinstance(self.still, Exception):
                raise self.still
            return {"result": {"value": self.still}}
        if method == "DOM.resolveNode":
            if not self.owner:
                raise RuntimeError("No node with given id found")
            return {"object": {"objectId": f"owner-{params['backendNodeId']}"}}
        if method == "DOM.describeNode" and params.get("objectId") == "el" and self.node:
            return {"node": {"backendNodeId": self.node}}
        return super().default(method, params, session)


SUNDAY = [_row("BESTÄTIGEN", i=0), _row("JETZT KAUFEN", i=1)]
TOP_CONFIRM, TOP_BUY = 2, 3  # the card frame's two rows come first


def _sunday(rows=None, prep=(True,)):
    sup = SundaySup(rows or SUNDAY, prep)
    assert surface.claim_payment_surface(sup, _hosted()) is None
    surface.record_approved_total(sup, *APPROVED)
    return sup


def _click(sup, index):
    return surface.payment_click(sup, surface.payment_controls(sup)["controls"][index]["ref"])


def test_an_unwatched_confirm_stays_the_pay_press():
    """F r3: when the leave watch cannot be installed, a submit that starts after the press could not be seen: the
    confirm stays the approval's Pay press and the purchase is refused."""
    sup = _sunday()
    sup.watch = False
    first = _click(sup, TOP_CONFIRM)
    assert first["pay_pressed"] is True and sup.watched == 1
    assert _click(sup, TOP_BUY)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 1


def test_a_navigation_chrome_saw_start_after_the_confirm_rebooks_it_as_pay():
    """F r3 (Codex P1): a submit still in flight at the read, hidden from the in-page leave watch, is seen through
    Chrome's navigation-start events: the confirm counts as the Pay press and the purchase is refused."""
    sup = _sunday()
    sup.started_navigating = True
    first = _click(sup, TOP_CONFIRM)
    assert first["pay_pressed"] is True and first["counted_as_pay"] == "navigated"
    assert _click(sup, TOP_BUY)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 1


def test_a_navigation_that_starts_after_the_verdict_refuses_the_purchase():
    """F r4 (Codex P1): the confirm was taken back, then Chrome saw its submit start navigating: the purchase press is
    refused (the confirm was the Pay press after all) and nothing more is pressed."""
    sup = _sunday()
    first = _click(sup, TOP_CONFIRM)
    assert first["pay_pressed"] is False
    sup.started_navigating = True
    assert _click(sup, TOP_BUY)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 1 and surface.pay_press(sup) is not None


def test_a_background_submit_after_the_verdict_refuses_the_purchase():
    """F r5 (Codex delta 3): the confirm was taken back, then its form was submitted by script without a navigation:
    the kept watch state, read right before the purchase press, refuses it; nothing more is pressed."""
    sup = _sunday()
    assert _click(sup, TOP_CONFIRM)["pay_pressed"] is False
    sup.left = True
    assert _click(sup, TOP_BUY)["error_type"] == surface.ALREADY_PRESSED
    assert sup.watch_reads == 1 and len(_mouse_presses(sup)) == 1


def test_navigation_start_events_are_recorded_except_same_document_ones():
    from tools.browser_supervisor_frames import FrameTrackingMixin
    import threading

    class F(FrameTrackingMixin):
        def __init__(self):
            self._state_lock = threading.Lock()

    f = F()
    f._on_frame_loading({"frameId": "TOP", "navigationType": "sameDocument"})
    assert f.load_started_since("TOP", 0) is False
    f._on_frame_loading({"frameId": "TOP", "reason": "formSubmissionPost"})
    assert f.load_started_since("TOP", 0) is True and f.load_started_since("OTHER", 0) is False


def test_sunday_the_method_confirm_is_preparatory_and_the_purchase_is_the_one_pay_press():
    sup = _sunday()
    sup.node = 4242
    first = _click(sup, TOP_CONFIRM)
    assert first["success"] is True and first["pay_pressed"] is False and first["next"] == surface.PREP_CONFIRM_NEXT
    assert isinstance(first["effect"], dict) and first["effect"]["unchanged"] is not None
    assert len(_mouse_presses(sup)) == 1 and surface.presses_in_lock(sup) == 1
    assert surface.pay_press(sup) is None, "the provisional Pay booking was taken back"
    # the claimed card frame's owner, by frame id, resolved into the listing's own isolated world (5)
    assert sup.sent("DOM.getFrameOwner")[0][1] == {"frameId": "CARD"}
    assert sup.sent("DOM.resolveNode")[0][1] == {"backendNodeId": 77, "executionContextId": 5}
    arr, idx, *owners = sup.prep_calls[0]
    assert arr == {"objectId": "arr"} and idx == {"value": [1]} and owners == [{"objectId": "owner-77"}]
    assert len(sup.prep_calls) == 2, "read when the click is judged and again right before the press"
    second = _click(sup, TOP_BUY)
    assert second["success"] is True and second["pay_pressed"] is True
    assert surface.pay_press(sup) == {"caption": "JETZT KAUFEN", "frame": "PAGE", "loader": "LP", "node": 4242,
                                      "key": "https://shop.test/"}
    for index in (TOP_BUY, TOP_CONFIRM):  # a third press, the purchase or the confirm: refused, nothing pressed
        assert _click(sup, index)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 2 and surface.presses_in_lock(sup) == 2


@pytest.mark.parametrize("case", ["lone confirm", "named confirm", "card frame", "same box", "unread", "no owner",
                                  "wallet outside", "link outside", "disabled outside"])
def test_a_confirm_stays_the_pay_press_without_positive_evidence(case):
    """Fails closed: a confirm that is the only pay-like control, one that names the purchase, one in the hosted card
    frame, one whose box also holds the purchase button, an unread page script, a card frame owner that does not
    resolve, or an outside control that is a wallet button, a link or disabled: the Pay press."""
    rows = {"lone confirm": [_row("BESTÄTIGEN")],
            "named confirm": [_row("Zahlung bestätigen", i=0), _row("JETZT KAUFEN", i=1)],
            # Opus probe: "Buy with Apple Pay" above a Stripe form whose "Submit" places the order
            "wallet outside": [_row("Submit", i=0), _row("Buy with Apple Pay", i=1)],
            "link outside": [_row("BESTÄTIGEN", i=0), _row("JETZT KAUFEN", role="link", i=1)],
            "disabled outside": [_row("BESTÄTIGEN", i=0), {**_row("JETZT KAUFEN", i=1), "disabled": True}],
            }.get(case, SUNDAY)
    prep = {"same box": (False,), "unread": (RuntimeError("Execution context was destroyed"),)}.get(case, (True,))
    sup = _sunday(rows, prep)
    sup.owner = case != "no owner"
    index = 0 if case == "card frame" else len(rows)
    out = _click(sup, index)
    assert out["success"] is True and out["pay_pressed"] is True, case
    if case not in ("same box", "unread"):
        assert sup.prep_calls == [], "the page script is not even read"
    again = TOP_BUY if case in ("same box", "unread", "no owner", "card frame") else index
    assert _click(sup, again)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 1


def test_a_confirm_whose_box_changes_before_the_press_is_not_pressed():
    sup = _sunday(prep=(True, False))  # judged preparatory, then the purchase button moved into its box
    out = _click(sup, TOP_CONFIRM)
    assert out["error_type"] == "payment_control_changed" and not _mouse_presses(sup)
    assert surface.presses_in_lock(sup) == 0 and surface.pay_press(sup) is None


def test_a_booking_modals_confirm_reservation_is_the_pay_press():
    """Codex/Opus probe: a booking modal whose "Confirm reservation" charges the card, "Reserve" on the page: the
    confirm names the purchase now, so it is the Pay press and "Reserve" is refused (one submission)."""
    sup = _sunday([_row("Confirm reservation", i=0), _row("Reserve", i=1)])
    assert _click(sup, TOP_CONFIRM)["pay_pressed"] is True and sup.prep_calls == []
    assert _click(sup, TOP_BUY)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 1


def test_only_one_preparatory_press_per_approval():
    """Opus probe: a preparatory confirm repeated without limit. The second confirm-only press is the Pay press."""
    sup = _sunday()
    assert _click(sup, TOP_CONFIRM)["pay_pressed"] is False
    second = _click(sup, TOP_CONFIRM)
    assert second["pay_pressed"] is True and second["next"] == surface.PAY_PRESSED_NEXT
    assert len(sup.prep_calls) == 2, "the second press is not even judged"
    assert _click(sup, TOP_BUY)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 2


@pytest.mark.parametrize("case, counted", [("navigated", "navigated"), ("gone", "purchase_button_gone"),
                                           ("unread", "purchase_button_gone"), ("effect", "effect_unread")])
def test_a_preparatory_press_that_changed_the_checkout_is_rebooked_as_the_pay_press(case, counted, monkeypatch):
    sup = _sunday()
    if case == "navigated":  # the top document changed: the confirm may have placed the order
        from tests.tools.test_browser_payment_surface import TREE

        sup.after = {"loader": "LP2", "listed": (("PAGE", "LP2", *TREE[0][2:]), *TREE[1:])}
    elif case == "gone":  # JETZT KAUFEN is no longer listed, or is disabled
        sup.still = False
    elif case == "unread":
        sup.still = RuntimeError("Execution context was destroyed")
    else:
        async def _none(*_a, **_k):
            return None

        monkeypatch.setattr(surface, "_bounded_effect", _none)
    out = _click(sup, TOP_CONFIRM)
    assert out["pay_pressed"] is True and out["counted_as_pay"] == counted and out["next"] == surface.PREP_COUNTED_NEXT
    assert surface.pay_press(sup)["caption"] == "BESTÄTIGEN"
    assert _click(sup, TOP_BUY)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 1


def test_a_preparatory_press_cut_off_before_its_verdict_stays_the_pay_press(monkeypatch):
    """The press is booked as the Pay press at its dispatch; only a verdict read after it takes that back."""
    async def _cut(*_a, **_k):
        raise RuntimeError("the click's wait ended")

    monkeypatch.setattr(surface, "prep_verdict", _cut)
    sup = _sunday()
    _click(sup, TOP_CONFIRM)
    assert len(_mouse_presses(sup)) == 1 and surface.pay_press(sup)["caption"] == "BESTÄTIGEN"
    assert _click(sup, TOP_BUY)["error_type"] == surface.ALREADY_PRESSED


# ------------------------------------------------------------------ payment_done needs order evidence

SHOP = done_tests.SHOP
CHECKOUT = done_tests.CHECKOUT  # TOP (L0) at /checkout with the card frame
SAME_DOC = (("TOP", "L0", SHOP + "/checkout/", SHOP, ""),)  # the card frames gone, the same document
CART = {"title": "Zur Kasse", "url": SHOP + "/checkout/", "text": "Warenkorb 16 Pakete Summe 22,66 EUR Jetzt kaufen"}
THANKS = {"title": "Bestellbestätigung", "url": SHOP + "/checkout/", "text": "Vielen Dank für Ihre Bestellung!"}


class PaySup(done_tests._Unlocking):
    """``node``: the backendNodeId the Pay press's control reads as; ``shown``: whether that element is still
    connected and rendered (None: the node no longer resolves)."""

    node, shown = None, None

    def answer(self, method, params, session_id=None):
        if method == "DOM.describeNode" and params.get("objectId") == "el" and self.node:
            return {"node": {"backendNodeId": self.node}}
        if method == "DOM.resolveNode" and params.get("backendNodeId") == self.node:
            if self.shown is None:
                raise RuntimeError("CDP error: No node with given id found")
            return {"object": {"objectId": "pay-el"}}
        if method == "Runtime.callFunctionOn" and params["functionDeclaration"] == evidence._SHOWN:
            return {"result": {"value": self.shown}}
        return super().answer(method, params, session_id)


def _pressed(pay="JETZT KAUFEN", node=None):
    """The Sunday checkout claimed with EUR 22.66 bound, and its purchase control pressed once (the Pay press)."""
    sup = done_tests._claimed(PaySup([CHECKOUT], status=dict(done_tests.LOCKED_TAB)))
    sup.node = node
    surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), SHOP)
    sup.rows = [_row(pay)]
    surface.payment_controls(sup)
    ref = done_tests._top_ref(sup, CHECKOUT)
    assert surface.payment_click(sup, ref)["pay_pressed"] is True
    return sup


def _not_reached(out, reason):
    assert out["error_type"] == "payment_confirmation_not_reached" and out["reason"] == reason, out
    assert out["lock_released"] is False


@pytest.mark.parametrize("page, rows, reason", [
    (CART, [_row("JETZT KAUFEN")], "no_order_wording"),  # the live failure: the checkout, its Pay still there
    (THANKS, [_row("JETZT KAUFEN")], "purchase_button_shown"),  # order wording, an enabled purchase button listed
    (CART, [_row("Weiter einkaufen", role="link")], "no_order_wording"),
    # Opus probe: "Danke" in the checkout's footer with JETZT KAUFEN listed is no order wording
    ({**CART, "text": CART["text"] + " Danke, dass du sunday vertraust"}, [_row("JETZT KAUFEN")], "no_order_wording"),
])
def test_the_checkout_document_is_no_confirmation_without_order_evidence(page, rows, reason):
    sup = _pressed()
    sup.trees, sup.rows = [SAME_DOC], rows
    sup.page.update(page)
    _not_reached(done.payment_done(sup), reason)
    assert not sup.sent("Target.createTarget") and not sup.sent("Target.closeTarget")
    assert surface.presses_in_lock(sup) == 1


def test_the_checkout_document_with_order_wording_and_no_pay_control_is_the_confirmation():
    sup = _pressed()
    sup.trees, sup.rows = [SAME_DOC], [_row("Weiter einkaufen", role="link")]
    sup.page.update(THANKS)
    out = done.payment_done(sup)
    assert out["success"] is True and out["order_evidence"] == "confirmation_text"
    assert out["lock_released"] is True and out["new_tab"] == "NEWTAB" and "switch_tab(new_tab)" in out["next"]


@pytest.mark.parametrize("url", [SHOP + "/checkout/", SHOP + "/checkout/finish"])
def test_failure_wording_is_never_a_confirmation(url):
    """Codex P1: "Zahlung fehlgeschlagen / Es wurde keine Bestellung aufgegeben" with Retry in place of Pay returned
    success (the order regex matched the negated "Bestellung aufgegeben"); a new error document did too."""
    sup = _pressed()
    loader = "L0" if url.endswith("/checkout/") else "L7"
    sup.trees, sup.rows = [(("TOP", loader, url, SHOP, ""),)], [_row("Erneut versuchen")]
    sup.page.update(url=url, title="Zahlung", text="Zahlung fehlgeschlagen. Es wurde keine Bestellung aufgegeben.")
    _not_reached(done.payment_done(sup), "failure_wording")
    assert not sup.sent("Target.closeTarget")


def test_a_thank_you_page_with_a_recommended_jetzt_kaufen_is_the_confirmation():
    """Codex P1: a real confirmation listing "Jetzt kaufen" on recommended products stranded the lock."""
    sup = _pressed()
    url = SHOP + "/checkout/finish"
    sup.trees, sup.rows = [(("TOP", "L7", url, SHOP, ""),)], [_row("Jetzt kaufen")]
    sup.page.update(url=url, title="Vielen Dank", text="Vielen Dank für Ihre Bestellung. Empfehlungen: Jetzt kaufen")
    out = done.payment_done(sup)
    assert out["success"] is True and out["order_evidence"] == "new_document" and out["lock_released"] is True


@pytest.mark.parametrize("shown, reached", [(True, False), (False, True), (None, True)])
def test_the_pressed_pay_control_is_told_by_identity(shown, reached):
    """The same document after Pay (single page): order wording, only a recommendation link "Jetzt kaufen" listed.
    The pressed JETZT KAUFEN element itself still rendered: not yet; hidden, or no longer resolving: confirmed."""
    sup = _pressed(node=4242)
    assert surface.pay_press(sup)["node"] == 4242
    sup.shown = shown
    sup.trees, sup.rows = [SAME_DOC], [_row("Jetzt kaufen", role="link")]
    sup.page.update(THANKS)
    out = done.payment_done(sup)
    if reached:
        assert out["success"] is True and out["order_evidence"] == "confirmation_text"
    else:
        _not_reached(out, "pay_control_shown")
    resolved = sup.sent("DOM.resolveNode")
    assert resolved and resolved[0][1]["backendNodeId"] == 4242


@pytest.mark.parametrize("url, evidence_kind", [
    (SHOP + "/checkout/finish", "new_document"),  # a new document elsewhere on the shop
    (SHOP + "/checkout?step=done#x", None),  # a new document at the checkout's URL: no wording, no success
])
def test_a_new_document_is_judged_by_its_url(url, evidence_kind):
    sup = _pressed()
    sup.trees, sup.rows = [(("TOP", "L7", url, SHOP, ""),)], [_row("Weiter einkaufen", role="link")]
    sup.page.update(url=url, title="Sunday", text="Ihr Konto")
    out = done.payment_done(sup)
    if evidence_kind:
        assert out["success"] is True and out["order_evidence"] == evidence_kind
    else:
        _not_reached(out, "no_order_wording")


def test_order_wording_past_the_excerpt_still_counts():
    """Codex P2: wording after the snapshot's first 20,000 characters stranded the lock; the scan reads it all."""
    sup = _pressed()
    sup.trees, sup.rows = [SAME_DOC], [_row("Weiter einkaufen", role="link")]
    sup.page.update(title="Sunday", text="x" * done.MAX_TEXT)
    sup.full_text = "x" * 30_000 + " Ihre Bestellung ist bei uns eingegangen."
    out = done.payment_done(sup)
    assert out["success"] is True and out["order_evidence"] == "confirmation_text"


def test_without_a_recorded_pay_press_any_purchase_button_on_the_checkout_document_holds_it():
    sup = done_tests._paid([CHECKOUT, SAME_DOC])  # no press of this process recorded
    sup.rows = [_row("Pay now")]
    sup.page.update(THANKS)
    _not_reached(done.payment_done(sup), "purchase_button_shown")


def test_not_reached_says_what_the_tab_showed_without_claiming_a_checkout():
    sup = _pressed()
    sup.trees = [done_tests.ACS]  # the bank's 3-D Secure page until the deadline
    out = done.payment_done(sup)
    _not_reached(out, "not_shop_page")
    assert "still shows a checkout" not in out["error"] and set(evidence.REASONS) <= set(
        w.strip("(),.") for w in out["error"].split())


# ------------------------------------------------------------------ the wording (Python and the page script)

ORDER_PHRASES = [
    "Order #1234 confirmed", "Order 77 confirmed", "Your order is complete", "Your order has been placed",
    "Payment successful", "Zahlung erfolgreich", "Bestellung erfolgreich", "Ihre Bestellung ist bei uns eingegangen",
    "Ihre Bestellung wurde erfolgreich aufgegeben", "Bestellnummer: 4711", "Your order number is 77",
    "Numéro de commande : 77", "Número de pedido 77", "Numero d'ordine 77", "Bestelnummer 77",
    "Vielen Dank für Ihre Bestellung!", "Thank you for your order", "Votre commande a bien été enregistrée",
    "Pedido confirmado", "Ordine confermato", "Bestelling geplaatst", "Réservation confirmée",
]
NOT_ORDER = ["Danke", "Danke, dass du sunday vertraust", "Thank you!", "Merci", "Your order will be placed once you pay",
             "Ihre Bestellung wird bestätigt", "Jetzt kaufen", "Bestellung abschließen", "Zahlung bestätigen",
             "Warenkorb 16 Pakete Summe 22,66 EUR"]
FAILURE_PHRASES = [
    "Zahlung fehlgeschlagen", "Ihre Zahlung wurde abgelehnt", "Die Bestellung wurde nicht erfolgreich abgeschlossen",
    "Es wurde keine Bestellung aufgegeben", "Payment failed", "Your card was declined", "Order not placed",
    "Your order has not been completed", "No order was created", "An error occurred", "Le paiement a échoué",
    "Paiement refusé", "Pago fallido", "Pago rechazado", "Pagamento non riuscito", "Betaling mislukt",
]


@pytest.mark.parametrize("text", ORDER_PHRASES)
def test_order_phrases(text):
    assert evidence.wording("", text) == {"order": True, "failure": False}


@pytest.mark.parametrize("text", NOT_ORDER)
def test_bare_thanks_and_checkout_words_are_no_order_wording(text):
    assert evidence.wording("", text)["order"] is False


@pytest.mark.parametrize("text", ["Die Zahlung ist nicht möglich.", "Payment not successful", "Order not confirmed",
                                  "Your payment was not successful. Please try again."])
def test_payment_qualified_negatives_are_failure(text):
    """F r3 (Codex P1): the narrowed wording still catches these real failure pages."""
    assert evidence.wording("", text)["failure"] is True


@pytest.mark.parametrize("text", FAILURE_PHRASES)
def test_failure_phrases(text):
    assert evidence.wording("", text)["failure"] is True


@pytest.mark.parametrize("text", [
    "Vielen Dank für Ihre Bestellung! Bestellnummer 10023. Eine Änderung der Bestellung ist leider nicht mehr möglich.",
    "Thank you for your order! If you notice an error in your order, please contact us.",
    "Thank you for your order. No order confirmation email? Check your spam folder.",
])
def test_service_text_on_a_real_thank_you_page_is_no_failure(text):
    """F r3 (Opus final M2): ordinary service text is not failure wording; the order wording still counts."""
    assert evidence.wording("", text) == {"order": True, "failure": False}


def test_a_submit_that_started_after_the_preparatory_press_rebooks_it(tmp_path):
    """F r3 (Opus final M1): the leave watch marks a submit, unload or navigation that starts after the press, so an
    order still in flight when the effect is read re-books the confirm as the Pay press (STILL_LISTED answers False)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the page script")
    harness = (
        "const listeners = {}; const on = (k) => (t, f) => { (listeners[k + t] = listeners[k + t] || []).push(f); };\n"
        "const window = {addEventListener: on('w')}; const document = {addEventListener: on('d')};\n"
        "const getComputedStyle = () => ({visibility: 'visible', display: 'block'});\n"
        "const buy = {isConnected: true, disabled: false, getAttribute: () => null,\n"
        "  getBoundingClientRect: () => ({width: 10, height: 10})};\n"
        "const arr = [null, buy];\n"
        f"const still = {kind.STILL_LISTED};\n"
        f"const before = still.call(arr, [1]);\n"
        f"({kind.WATCH_LEAVE}).call(arr);\n"
        "const quiet = still.call(arr, [1]);\n"
        "for (const f of listeners['dsubmit'] || []) f();\n"
        "const after = still.call(arr, [1]);\n"
        "process.stdout.write(JSON.stringify([before, quiet, after]));")
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    # unwatched: counts as Pay; watched and quiet: still preparatory; a submit started: the Pay press after all
    assert json.loads(run.stdout) == [False, True, False]


def test_once_the_confirm_submitted_a_purchase_click_never_reaches_the_page():
    """F r7 (Codex delta 5): the watch blocks a press on a purchase button in the renderer as the click is dispatched,
    once the confirm submitted; before that, and on other elements, clicks pass."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the page script")
    harness = (
        "const ls = {}; const on = (k) => (t, f) => { (ls[k + t] = ls[k + t] || []).push(f); };\n"
        "const window = {addEventListener: on('w')}; const document = {addEventListener: on('d')};\n"
        "const el = () => { const e = {}; e.contains = x => x === e; return e; };\n"
        "const buy = el(), other = el(); const arr = [other, buy];\n"
        f"const s = ({kind.WATCH_LEAVE}).call(arr, [1]);\n"
        "const press = (target) => { const ev = {target, stopped: false, prevented: false,\n"
        "  preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }};\n"
        "  for (const f of ls['wclick'] || []) f(ev); return ev.prevented && ev.stopped; };\n"
        "const before = press(buy);\n"
        "for (const f of ls['dsubmit'] || []) f();\n"
        "const after = press(buy), elsewhere = press(other);\n"
        "process.stdout.write(JSON.stringify([before, after, elsewhere, s.blocked]));")
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [False, True, False, True]


_SCAN_DOM = r"""
const T = (data) => ({nodeType: 3, data});
const E = (tag, kids = [], o = {}) => Object.assign({nodeType: 1, tagName: tag, childNodes: kids,
  style: {display: 'block', visibility: 'visible'}}, o);
const getComputedStyle = (el) => el.style;
const scan = (title, kids) => { const document = {title, body: E('BODY', kids)}; return SCAN; };
"""


def test_the_page_script_scans_all_visible_text_and_no_form_value():
    cases = {
        "past the excerpt": "[E('P', [T('x'.repeat(30000))]), E('H1', [T('Bestellung erfolgreich')])]",
        "in a textarea": "[E('TEXTAREA', [T('Bestellnummer 77')])]",
        "hidden": "[E('DIV', [T('Bestellnummer 77')], {style: {display: 'none', visibility: 'visible'}})]",
        "editable": "[E('DIV', [T('Bestellnummer 77')], {isContentEditable: true})]",
        "failure": "[E('P', [T('Zahlung fehlgeschlagen.')])]",
        "accented": "[E('P', [T('Le paiement a échoué')])]",
    }
    script = _SCAN_DOM.replace("SCAN", evidence.SCAN_JS) + "process.stdout.write(JSON.stringify({" + ", ".join(
        f"{json.dumps(k)}: scan('Sunday', {v})" for k, v in cases.items()) + "}));"
    out = _node(script)
    assert out["past the excerpt"] == {"order": True, "failure": False}
    for key in ("in a textarea", "hidden", "editable"):
        assert out[key] == {"order": False, "failure": False}, key
    assert out["failure"]["failure"] is True and out["accented"]["failure"] is True
    phrases = _node(_SCAN_DOM.replace("SCAN", evidence.SCAN_JS) + "process.stdout.write(JSON.stringify(" + json.dumps(
        ORDER_PHRASES + NOT_ORDER + FAILURE_PHRASES) + ".map(t => scan('', [E('P', [T(t)])]))));")
    assert phrases == [evidence.wording("", t) for t in ORDER_PHRASES + NOT_ORDER + FAILURE_PHRASES]
