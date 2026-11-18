"""Build F (2026-11-18 Sunday live retry, Unzer UI v1): a "BESTÄTIGEN" that only confirmed the card in the payment-method
modal used the approval's one Pay press, the page's "JETZT KAUFEN" was then refused, and payment_done reported the
checkout itself as the order confirmation. A confirm-only press inside a dialog or the card's box, with the purchase
control outside it, is a preparatory step; payment_done needs order evidence on the checkout's own document or URL and
never succeeds while the pressed Pay caption is still listed. Fake supervisor and fake CDP only; synthetic data."""

import json
import shutil
import subprocess

import pytest

from tools import browser_payment_done as done
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


# ------------------------------------------------------------------ which captions only confirm


@pytest.mark.parametrize("caption, strong, confirm_only", [
    ("BESTÄTIGEN", False, True), ("Bestätigen", False, True), ("Confirm", False, True), ("Submit", False, True),
    ("Confirm payment method", False, True), ("Confirmer le moyen de paiement", False, True),
    ("Zahlung bestätigen", True, False), ("confirm payment", True, False), ("Bestellung bestätigen", True, False),
    ("confirm order", True, False), ("confirmer le paiement", True, False), ("confirmar pago", True, False),
    ("Kauf abschließen", True, False), ("JETZT KAUFEN", True, False), ("Pay 22.66 EUR", True, False),
    ("Zahlungspflichtig bestellen", True, False), ("Continue", False, False), (surface.CAPTION_WITHHELD, False, False),
])
def test_a_confirm_word_names_the_purchase_only_with_a_purchase_noun(caption, strong, confirm_only):
    from tools import browser_payment_press_kind as kind

    assert kind.strong_pay(caption) is strong and kind.confirm_only(caption) is confirm_only
    assert surface._pay_press("button", caption) is (caption not in ("Continue",)), "the Pay-like set is unchanged"


# ------------------------------------------------------------------ the page script (node)

_DOM = r"""
const E = (tag, kids = [], o = {}) => {
  const el = Object.assign({tagName: tag, kids, parentElement: null, isConnected: true, dialog: false, src: ''}, o);
  for (const k of kids) k.parentElement = el;
  el.getAttribute = n => (n === 'src' ? el.src : null);
  el.closest = () => { for (let n = el; n; n = n.parentElement) if (n.dialog) return n; return null; };
  el.contains = x => { for (let n = x; n; n = n.parentElement) if (n === el) return true; return false; };
  const all = n => n.kids.flatMap(k => [k, ...all(k)]);
  el.querySelectorAll = sel => all(el).filter(n => n.tagName.toUpperCase() === sel.toUpperCase());
  return el;
};
const location = {href: 'https://www.sunday.test/checkout/'};
const getComputedStyle = n => ({position: n.fixed ? 'fixed' : 'static'});
const card = () => E('iframe', [], {src: 'https://payment.unzer.test/card'});
const confirm = E('button'), buy = E('button');
"""
_LAYOUTS = {
    # Unzer's method modal: the card frames and "BESTÄTIGEN" in a dialog, "JETZT KAUFEN" on the page
    "method modal": ("[E('main', [buy]), E('div', [E('div', [card(), card()]), E('div', [confirm])], {dialog: true})]",
                     True),
    "card box, no dialog": ("[E('main', [buy]), E('section', [card(), E('div', [confirm])])]", True),
    "purchase in the same dialog": ("[E('div', [card(), confirm, buy], {dialog: true})]", False),
    "no box below body": ("[confirm, buy, card()]", False),
    "another site's iframe": ("[E('main', [buy]), E('section', [E('iframe', [], {src: 'https://ads.test/x'}), "
                              "confirm])]", False),
    # a plain fixed overlay modal whose card iframes carry no src yet (set by the provider's script later)
    "fixed overlay, no dialog, src-less frames": (
        "[E('main', [buy]), E('div', [E('div', [E('iframe'), E('iframe')]), E('div', [confirm])], {fixed: true})]",
        True),
    "fixed overlay holding the purchase too": ("[E('div', [E('iframe'), confirm, buy], {fixed: true})]", False),
}


@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_the_page_script_finds_the_purchase_control_outside_the_confirms_box(layout):
    from tools import browser_payment_press_kind as kind

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the page script")
    body, expected = _LAYOUTS[layout]
    harness = (_DOM + f"const body = E('body', {body}); const html = E('html', [body]);\n"
               "const document = {body, documentElement: html};\n"
               f"process.stdout.write(JSON.stringify(({kind._prep_check()}).call(confirm, [confirm, buy], [1], "
               "['https://payment.unzer.test'])));")
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) is expected


# ------------------------------------------------------------------ the Sunday checkout (payment_click)


class SundaySup(DialogSup, EffectSup):
    """The merchant top document lists ``rows`` (card frame first, then the top: indices offset by ``len(rows)``);
    ``prep``: what the page script answers on each read (the last one repeats; an exception: the read fails)."""

    def __init__(self, rows, prep=(True,)):
        super().__init__(rows)
        self.prep, self.prep_calls = list(prep), []

    def default(self, method, params, session):
        if method == "Runtime.callFunctionOn" and "cardBox" in params.get("functionDeclaration", ""):
            self.prep_calls.append(params["arguments"])
            answer = self.prep.pop(0) if len(self.prep) > 1 else self.prep[0]
            if isinstance(answer, Exception):
                raise answer
            return {"result": {"value": answer}}
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


def test_sunday_the_method_confirm_is_preparatory_and_the_purchase_is_the_one_pay_press():
    sup = _sunday()
    first = _click(sup, TOP_CONFIRM)
    assert first["success"] is True and first["pay_pressed"] is False and first["next"] == surface.PREP_CONFIRM_NEXT
    assert isinstance(first["effect"], dict) and first["effect"]["unchanged"] is not None
    assert len(_mouse_presses(sup)) == 1 and surface.presses_in_lock(sup) == 1
    # the script got the purchase control's index in the same listing, and the card origins
    arr, idx, origins = sup.prep_calls[0]
    assert arr == {"objectId": "arr"} and idx == {"value": [1]} and "https://psp.test" in origins["value"]
    assert len(sup.prep_calls) == 2, "read when the click is judged and again right before the press"
    second = _click(sup, TOP_BUY)
    assert second["success"] is True and second["pay_pressed"] is True
    assert surface.pay_caption(sup) == "JETZT KAUFEN"
    for index in (TOP_BUY, TOP_CONFIRM):  # a third press, the purchase or the confirm: refused, nothing pressed
        assert _click(sup, index)["error_type"] == surface.ALREADY_PRESSED
    assert len(_mouse_presses(sup)) == 2 and surface.presses_in_lock(sup) == 2


@pytest.mark.parametrize("case", ["lone confirm", "named confirm", "card frame", "same box", "unread"])
def test_a_confirm_stays_the_pay_press_unless_the_purchase_lies_outside_its_box(case):
    """Fails closed: a confirm that is the only pay-like control, a confirm that names the purchase, a confirm in the
    hosted card frame, one whose box also holds the purchase control, or an unread page script: the Pay press."""
    rows = {"lone confirm": [_row("BESTÄTIGEN")],
            "named confirm": [_row("Zahlung bestätigen", i=0), _row("JETZT KAUFEN", i=1)]}.get(case, SUNDAY)
    prep = {"same box": (False,), "unread": (RuntimeError("Execution context was destroyed"),)}.get(case, (True,))
    sup = _sunday(rows, prep)
    index = 0 if case == "card frame" else len(rows)
    out = _click(sup, index)
    assert out["success"] is True and out["pay_pressed"] is True, case
    if case in ("lone confirm", "named confirm", "card frame"):
        assert sup.prep_calls == [], "the page script is not even read"
    assert _click(sup, TOP_BUY if len(rows) == 2 else index)["error_type"] == surface.ALREADY_PRESSED


def test_a_confirm_whose_box_changes_before_the_press_is_not_pressed():
    sup = _sunday(prep=(True, False))  # judged preparatory, then the purchase control moved into its dialog
    out = _click(sup, TOP_CONFIRM)
    assert out["error_type"] == "payment_control_changed" and not _mouse_presses(sup)
    assert surface.presses_in_lock(sup) == 0


# ------------------------------------------------------------------ payment_done needs order evidence

SHOP = done_tests.SHOP
CHECKOUT = done_tests.CHECKOUT  # TOP (L0) at /checkout with the card frame
SAME_DOC = (("TOP", "L0", SHOP + "/checkout/", SHOP, ""),)  # the card frames gone, the same document
CART = {"title": "Zur Kasse", "url": SHOP + "/checkout/", "text": "Warenkorb 16 Pakete Summe 22,66 EUR Jetzt kaufen"}
THANKS = {"title": "Bestellbestätigung", "url": SHOP + "/checkout/", "text": "Vielen Dank für Ihre Bestellung!"}


def _pressed(pay="JETZT KAUFEN"):
    """The Sunday checkout claimed with EUR 22.66 bound, and its purchase control pressed once (the Pay press)."""
    sup = done_tests._claimed(done_tests._Unlocking([CHECKOUT], status=dict(done_tests.LOCKED_TAB)))
    surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), SHOP)
    sup.rows = [_row(pay)]
    surface.payment_controls(sup)
    ref = done_tests._top_ref(sup, CHECKOUT)
    assert surface.payment_click(sup, ref)["pay_pressed"] is True
    return sup


@pytest.mark.parametrize("page, rows", [
    (CART, [_row("JETZT KAUFEN")]),  # the live failure: the checkout, its Pay still there
    (THANKS, [_row("JETZT KAUFEN")]),  # thanks wording, but the pressed Pay caption is still listed
    (CART, [_row("Weiter einkaufen", role="link")]),  # no confirmation wording on the checkout's document
])
def test_the_checkout_document_is_no_confirmation_without_order_evidence(page, rows):
    sup = _pressed()
    sup.trees, sup.rows = [SAME_DOC], rows
    sup.page.update(page)
    out = done.payment_done(sup)
    assert out["error_type"] == "payment_confirmation_not_reached" and out["lock_released"] is False
    assert not sup.sent("Target.createTarget") and not sup.sent("Target.closeTarget")
    assert surface.presses_in_lock(sup) == 1


def test_the_checkout_document_with_order_wording_and_no_pay_control_is_the_confirmation():
    sup = _pressed()
    sup.trees, sup.rows = [SAME_DOC], [_row("Weiter einkaufen", role="link")]
    sup.page.update(THANKS)
    out = done.payment_done(sup)
    assert out["success"] is True and out["order_evidence"] == "confirmation_text"
    assert out["lock_released"] is True and out["new_tab"] == "NEWTAB" and "switch_tab(new_tab)" in out["next"]


@pytest.mark.parametrize("url, evidence", [
    (SHOP + "/checkout/finish", "new_document"),  # a new document elsewhere on the shop
    (SHOP + "/checkout?step=done#x", None),  # a new document at the checkout's URL: no wording, no success
])
def test_a_new_document_is_judged_by_its_url(url, evidence):
    sup = _pressed()
    sup.trees, sup.rows = [(("TOP", "L7", url, SHOP, ""),)], [_row("Weiter einkaufen", role="link")]
    sup.page.update(url=url, title="Sunday", text="Ihr Konto")
    out = done.payment_done(sup)
    if evidence:
        assert out["success"] is True and out["order_evidence"] == evidence
    else:
        assert out["error_type"] == "payment_confirmation_not_reached" and out["lock_released"] is False


def test_a_new_document_still_showing_the_pressed_pay_caption_is_no_confirmation():
    sup = _pressed()
    url = SHOP + "/checkout/review"
    sup.trees, sup.rows = [(("TOP", "L7", url, SHOP, ""),)], [_row("Jetzt  kaufen")]
    sup.page.update(url=url, title="Danke", text="Vielen Dank")
    assert done.payment_done(sup)["error_type"] == "payment_confirmation_not_reached"


def test_without_a_recorded_pay_caption_any_purchase_control_on_the_checkout_document_holds_it():
    sup = done_tests._paid([CHECKOUT, SAME_DOC])  # no press of this process recorded
    sup.rows = [_row("Pay now")]
    sup.page.update(THANKS)
    assert done.payment_done(sup)["error_type"] == "payment_confirmation_not_reached"
