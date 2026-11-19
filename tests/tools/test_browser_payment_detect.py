"""Build G1 (owner direction 2026-11-19, after the Sunday live retry of 2026-11-18, Unzer UI v1): no press budget. An
approval allows any presses; after a Pay-like press Hermes looks for order evidence, and a detected payment spends the
approval (later Pay-like presses refused, payment_done succeeds). One guard: a Pay-like press while the last one's
navigation is still loading. Build F (kept): payment_done needs order evidence, rejects failure wording and tells the
pressed Pay control by identity. Fake supervisor and fake CDP only; synthetic data."""

import json
import shutil
import subprocess

import pytest

from tools import browser_payment_done as done
from tools import browser_payment_order_evidence as evidence
from tools import browser_payment_press_kind as kind
from tools import browser_payment_surface as surface
from tests.tools import test_browser_payment_done as done_tests
from tests.tools.test_browser_payment_surface_honesty import _mouse_presses, _row
from tools import browser_payment_detect as detect
from tools.browser_supervisor_frames import FrameTrackingMixin


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


# ------------------------------------------------------------------ which captions name the purchase


@pytest.mark.parametrize("caption, strong, _confirm", [
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
def test_a_confirm_word_names_the_purchase_only_with_a_purchase_noun(caption, strong, _confirm):
    """``strong_pay`` decides which listed buttons are purchase buttons (payment_done, the detection); every one of
    these captions but Continue is Pay-like (followed by the detection)."""
    assert kind.strong_pay(caption) is strong
    assert surface._pay_press("button", caption) is (caption not in ("Continue",)), "the Pay-like set is unchanged"


@pytest.mark.parametrize("control, purchase", [
    (_row("JETZT KAUFEN"), True), (_row("Place order"), True), (_row("Complete checkout"), True),
    (_row("JETZT KAUFEN", role="link"), False),  # a link (a product tile) is never the checkout's order button
    ({**_row("JETZT KAUFEN"), "disabled": True}, False),
    (_row("Buy with Apple Pay"), False), (_row("Google Pay"), False), (_row("Mit PayPal bezahlen"), False),
    (_row("Amazon Pay"), False), (_row("Klarna Sofort bezahlen"), False), (_row("Pay later"), False),
    (_row("Gutschein kaufen"), False), (_row("Buy a gift card"), False), (_row("Bestätigen"), False),
])
def test_only_an_enabled_non_wallet_purchase_button_counts(control, purchase):
    assert kind.purchase_button(control) is purchase


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
    # G1 (Opus L2): German payment errors
    "Es ist ein Fehler aufgetreten", "Bei der Zahlung ist ein Fehler aufgetreten", "Fehler bei der Zahlung",
    "Zahlung nicht autorisiert", "Ihre Zahlung wurde nicht akzeptiert", "Zahlung konnte nicht verarbeitet werden",
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
    # G1 (Opus L3): service text that read as failure before
    "Vielen Dank für Ihre Bestellung! Ihre E-Mail-Adresse ist noch nicht bestätigt.",
    "Vielen Dank für Ihre Bestellung! Änderungen können nicht mehr bearbeitet werden.",
    "Thank you for your order! Your account has not been completed yet.",
])
def test_service_text_on_a_real_thank_you_page_is_no_failure(text):
    """F r3 (Opus final M2): ordinary service text is not failure wording; the order wording still counts."""
    assert evidence.wording("", text) == {"order": True, "failure": False}


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


# ------------------------------------------------------------------ G1: detect the payment, no press budget

SUNDAY_ROWS = [_row("BESTÄTIGEN"), _row("JETZT KAUFEN", i=1)]
DECLINED = {**CART, "text": CART["text"] + " Zahlung fehlgeschlagen. Bitte versuche es erneut."}
SAME_DOC_CARD = (("TOP", "L0", SHOP + "/checkout/", SHOP, ""), done_tests.CHECKOUT[1])  # card frame still in the tree


class ShopSup(PaySup):
    """The Sunday checkout as the payment tab shows it: ``after`` (applied when a press is released) changes what the
    page shows (``page``, ``rows``, ``tree``); navigation start and stop events go through the real
    ``FrameTrackingMixin`` handlers (``loading_since``)."""

    loading_since = FrameTrackingMixin.loading_since
    _on_frame_loading = FrameTrackingMixin._on_frame_loading
    _on_frame_stopped = FrameTrackingMixin._on_frame_stopped

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.after, self.rows, self.tree = {}, list(SUNDAY_ROWS), CHECKOUT

    async def _payment_tree(self, sid, child_sessions):
        return self.tree  # the page changes only through ``after`` (or the test)

    def answer(self, method, params, session_id=None):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
            for key, value in self.after.items():
                if key == "page":
                    self.page.update(value)
                else:
                    setattr(self, key, value)
            self.after = {}
        return super().answer(method, params, session_id)


def _sunday():
    sup = done_tests._claimed(ShopSup([CHECKOUT], status=dict(done_tests.LOCKED_TAB)))
    surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), SHOP)
    sup.page.update(CART)
    return sup


def _press(sup, caption, after=None):
    """List, then press the merchant top document's control ``caption``; ``after``: what the press makes the page show."""
    surface.payment_controls(sup)
    with surface._LOCK:
        issued = dict(surface._ISSUED.get(sup) or {})
    key = f"{sup.tree[0][0]}|{sup.tree[0][1]}"
    ref = next(r for r, spec in issued.items() if spec[0] == key and spec[3] == caption)
    sup.after = after or {}
    return surface.payment_click(sup, ref)


def test_sunday_the_method_confirm_and_the_purchase_both_go_and_the_thank_you_spends_the_approval():
    """The live failure (2026-11-18): BESTÄTIGEN in the method modal, then JETZT KAUFEN. Both are pressed (no budget);
    the thank-you page is detected after the purchase press; a later Pay-like press is refused as detected; payment_done
    succeeds with order evidence."""
    sup = _sunday()
    first = _press(sup, "BESTÄTIGEN")  # no navigation, the checkout still shows the cart and JETZT KAUFEN
    assert first["success"] is True and first["pay_pressed"] is True and first["payment_detected"] is False
    assert first["next"] == surface.PAY_PRESSED_NEXT and "never" not in first["next"]
    second = _press(sup, "JETZT KAUFEN", after={"page": THANKS, "rows": [_row("Weiter einkaufen", role="link"),
                                                                        _row("Jetzt kaufen", role="link", i=1)]})
    assert second["success"] is True and second["pay_pressed"] is True and second["payment_detected"] is True
    assert second["next"] == surface.DETECTED_NEXT and len(_mouse_presses(sup)) == 2
    assert detect.detected(sup) == {"where": "top", "frame": "TOP", "loader": "L0", "evidence": "confirmation_text",
                                    "caption": "JETZT KAUFEN"}
    calls = len(sup.calls)
    third = _press(sup, "Jetzt kaufen")  # a recommendation's Pay-like control on the thank-you page
    assert third["success"] is False and third["error_type"] == detect.SPENT
    assert "payment was detected" in third["error"] and "JETZT KAUFEN" in third["error"]
    assert "payment_done" in third["error"] and "new approval" in third["error"]
    assert len(_mouse_presses(sup)) == 2 and not [c for c in sup.calls[calls:] if c[0] == "Input.dispatchMouseEvent"]
    assert _press(sup, "Weiter einkaufen")["success"] is True, "a press that is not Pay-like still goes"
    out = done.payment_done(sup)
    assert out["success"] is True and out["order_evidence"] == "confirmation_text" and out["lock_released"] is True


def test_a_retry_after_a_decline_on_the_same_document_goes_through():
    """No press budget: a declined payment re-renders the checkout with failure wording; JETZT KAUFEN again is
    pressed (its total read again), and nothing is detected."""
    from tests.tools.test_browser_payment_surface import _total_reads

    sup = _sunday()
    assert _press(sup, "JETZT KAUFEN", after={"page": DECLINED})["payment_detected"] is False
    reads = len(_total_reads(sup))
    again = _press(sup, "JETZT KAUFEN")
    assert again["success"] is True and again["pay_pressed"] is True and again["payment_detected"] is False
    assert len(_mouse_presses(sup)) == 2 and len(_total_reads(sup)) > reads
    assert surface.presses_in_lock(sup) == 2 and detect.detected(sup) is None


def test_a_changed_total_still_refuses_a_second_pay_press():
    sup = _sunday()
    assert _press(sup, "JETZT KAUFEN", after={"page": DECLINED})["success"] is True
    sup.pin = {"state": "matched", "labelled": [2999], "stated": 1, "unclear": False}  # the cart now asks 29.99
    out = _press(sup, "JETZT KAUFEN")
    assert out["error_type"] == surface.TOTAL_CHANGED and len(_mouse_presses(sup)) == 1


def test_a_pay_press_while_the_last_ones_navigation_loads_is_refused_until_the_new_document_is_ready():
    """The in-flight guard: Chrome reported a navigation of the top frame that started after the Pay press and has
    not finished loading; the next Pay-like press is refused with nothing pressed (the ref stays), and goes once the
    load stopped. A same-document navigation never holds it."""
    sup = _sunday()
    assert _press(sup, "JETZT KAUFEN", after={"page": DECLINED})["success"] is True
    sup._on_frame_loading({"frameId": "TOP", "navigationType": "differentDocument"})
    surface.payment_controls(sup)
    with surface._LOCK:
        ref = next(r for r, spec in surface._ISSUED[sup].items() if spec[0] == "TOP|L0" and spec[3] == "JETZT KAUFEN")
    held = surface.payment_click(sup, ref)
    assert held["error_type"] == detect.INFLIGHT and "still processing the last Pay press" in held["error"]
    assert len(_mouse_presses(sup)) == 1
    sup._on_frame_stopped({"frameId": "TOP"})  # the new document committed and loaded
    assert surface.payment_click(sup, ref)["success"] is True, "the refused press kept its ref"
    sup._on_frame_loading({"frameId": "TOP", "navigationType": "differentDocument"})
    assert _press(sup, "BESTÄTIGEN")["error_type"] == detect.INFLIGHT  # any Pay-like press
    sup._on_frame_stopped({"frameId": "TOP"})
    sup._on_frame_loading({"frameId": "TOP", "navigationType": "historySameDocument"})
    assert _press(sup, "JETZT KAUFEN")["success"] is True and len(_mouse_presses(sup)) == 3


def test_the_in_flight_guard_is_bounded(monkeypatch):
    sup = _sunday()
    assert _press(sup, "JETZT KAUFEN", after={"page": DECLINED})["success"] is True
    sup._on_frame_loading({"frameId": "TOP", "navigationType": "differentDocument"})
    monkeypatch.setattr(detect, "_INFLIGHT_S", 0.0)
    assert _press(sup, "JETZT KAUFEN")["success"] is True


def test_navigation_events_ignore_same_document_and_other_tabs():
    sup = ShopSup([CHECKOUT])
    for params in ({"frameId": "TOP", "navigationType": "sameDocument"},
                   {"frameId": "TOP", "navigationType": "historySameDocument"},
                   {"frameId": "TOP", "disposition": "newTab", "reason": "anchorClick"}):
        sup._on_frame_loading(params)
        assert sup.loading_since("TOP", 0.0) is False, params
    sup._on_frame_loading({"frameId": "TOP", "reason": "formSubmissionPost", "disposition": "currentTab"})
    assert sup.loading_since("TOP", 0.0) is True and sup.loading_since("OTHER", 0.0) is False
    sup._on_frame_stopped({"frameId": "TOP"})
    assert sup.loading_since("TOP", 0.0) is False
    assert "Page.frameStoppedLoading" in FrameTrackingMixin.EVENT_HANDLERS
    assert "Page.navigatedWithinDocument" in FrameTrackingMixin.EVENT_HANDLERS


def test_a_bank_step_and_a_confirm_dialog_after_pay_still_go():
    """After the purchase press: the shop's own confirm dialog ("Bestellung bestätigen"), then the bank's 3-D Secure
    page (another site: no merchant total, never order evidence) with its "Bestätigen", are pressed."""
    from tests.tools.test_browser_payment_surface import _total_reads

    sup = _sunday()
    assert _press(sup, "JETZT KAUFEN", after={"rows": [_row("Bestellung bestätigen")]})["success"] is True
    assert _press(sup, "Bestellung bestätigen", after={"tree": done_tests.ACS, "rows": [_row("Bestätigen")]})[
        "payment_detected"] is False
    sup.page.update(title="Bank", text="Zahlung erfolgreich bestätigt")  # a bank page's wording never spends
    reads = len(_total_reads(sup))
    out = _press(sup, "Bestätigen")
    assert out["success"] is True and out["pay_pressed"] is True and out["payment_detected"] is False
    assert len(_total_reads(sup)) == reads and len(_mouse_presses(sup)) == 3 and detect.detected(sup) is None


def test_payment_done_succeeds_on_the_detected_document_with_the_card_frames_still_in_it():
    """A single-page shop whose thank-you keeps the provider's (cross-origin) card frame in its tree: payment_done's
    own shop-page rule refuses that document, but the detection already spent the approval on it."""
    sup = _sunday()
    out = _press(sup, "JETZT KAUFEN", after={"tree": SAME_DOC_CARD, "page": THANKS,
                                             "rows": [_row("Weiter einkaufen", role="link")]})
    assert out["payment_detected"] is True
    done_out = done.payment_done(sup)
    assert done_out["success"] is True and done_out["order_evidence"] == "confirmation_text"
    fresh = _sunday()  # no detection in this process: the same page stays refused
    fresh.tree = SAME_DOC_CARD
    fresh.page.update(THANKS)
    fresh.rows = [_row("Weiter einkaufen", role="link")]
    _not_reached(done.payment_done(fresh), "not_shop_page")


def test_a_confirmation_in_a_tab_the_pay_press_opened_is_detected():
    sup = _sunday()
    real = sup.answer

    def answer(method, params, session_id=None):
        if method == "Target.getTargets" and sup.popup:
            infos = real(method, params, session_id)
            return {"targetInfos": infos["targetInfos"] + [
                {"targetId": "POP", "type": "page", "openerId": "TAB", "url": SHOP + "/danke"}]}
        if method == "Page.getFrameTree" and session_id == "S-temp":
            return done_tests.frame_tree((("POPTOP", "LQ", SHOP + "/danke", SHOP, ""),))
        if method == "Runtime.evaluate" and params.get("expression") == evidence.SCAN_JS and session_id == "S-temp":
            return {"result": {"type": "object", "value": {"order": True, "failure": False}}}
        return real(method, params, session_id)

    sup.answer, sup.popup = answer, False
    out = _press(sup, "JETZT KAUFEN", after={"popup": True})
    assert out["payment_detected"] is True
    assert detect.detected(sup)["where"] == "new_tab" and detect.detected(sup)["tab"] == "POP"
    assert _press(sup, "JETZT KAUFEN")["error_type"] == detect.SPENT


def test_detection_never_reads_a_page_value():
    """The detection's page reads are the value-free scan, the identity read and the controls listing only."""
    sup = _sunday()
    _press(sup, "JETZT KAUFEN", after={"page": THANKS, "rows": [_row("Weiter einkaufen", role="link")]})
    evaluated = {c[1].get("expression") for c in sup.sent("Runtime.evaluate")}
    assert evidence.SCAN_JS in evaluated and done.SNAPSHOT_JS not in evaluated
