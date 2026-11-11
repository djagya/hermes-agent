"""Track B: owner-approved card fill on unreviewed hosted payment forms (design v2 §4, v1 §3/§9).

No browser and no real card: fake supervisors and fake CDP for the Python checks, Node for the page-side
closures (_EXPOSED, the discovery _INSPECT, _AMOUNT_WITNESS). Synthetic card values only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from agent.vault_payment_policy import PaymentDestinationError, PaymentPolicyError
from tools import browser_supervisor_payment as sp
from tools import browser_use_cli
from tools import browser_vault_payment as bvp
from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection

MERCHANT = "https://shop.example"
KPS = "https://kps.example"  # synthetic unreviewed hosted form (secure.kps-payment.de shape)
AD = "https://ads.example"
STRIPE = "https://js.stripe.com"
CARD = {"card_number": "4242424242424242", "exp_month": "07", "exp_year": "2035", "cvc": "837",
        "cardholder_name": "Ada Lovelace"}
MARKERS = ("4242424242424242", "4242 4242 4242 4242", "837")
CART = {"items": [{"description": "Adult ticket, row 6", "quantity": 2}], "total": "22.66", "currency": "EUR"}


def _controls(*tokens):
    return tuple(classify_checkout_control(LoginControl.from_dict({"index": n, "autocomplete": t}))
                 for n, t in enumerate(tokens))


def _candidate(origin, *tokens, frame="f"):
    return (f"{frame}-{origin}", f"s-{frame}", f"o-{frame}", origin, _controls(*tokens))


def _desc(index, autocomplete, **extra):
    """A complete discovery descriptor (describeOne's exact shape)."""
    return {"index": index, "autocomplete": autocomplete, "name": f"f{index} ", "label": " ", "type": "text",
            "formIndex": None, "maxLength": None, **extra}


def _rows(*frames):
    return {r[0]: r for r in (("m", "", MERCHANT + "/checkout", MERCHANT, ""), *frames)}


# ---------------------------------------------------------------------------------------------- B1 admission

_KPS_URL = KPS + "/customer/wallet/pc/WalletInput.jsp?t=x"


@pytest.mark.parametrize("frames, target, admitted", [
    ((("k", "", _KPS_URL, KPS, "m"),), "k", True),
    ((("k", "", KPS + "/outer", KPS, "m"), ("i", "", _KPS_URL, KPS, "k")), "i", True),  # same origin nested once
    ((("k", "", "http://kps.example/x", "http://kps.example", "m"),), "k", False),
    ((("k", "", "https://kps.example/x", "null", "m"),), "k", False),
    ((("k", "", "about:blank", KPS, "m"),), "k", False),
    ((("k", "", "about:srcdoc", KPS, "m"),), "k", False),
    ((("a", "", AD + "/slot", AD, "m"), ("k", "", _KPS_URL, KPS, "a")), "k", False),  # under an ad wrapper
    ((("p", "", "https://www.paypal.com/smart/buttons", "https://www.paypal.com", "m"),), "p", False),  # registry
    ((("p", "", "https://secure.pay1.de/x", "https://secure.pay1.de", "m"),), "p", False),  # registry, off (D5)
    ((("c", "", "https://www.computop-paygate.com/x", "https://www.computop-paygate.com", "m"),), "c", False),
    ((("k", "", "https://kps.example:443/x", "https://kps.example:443", "m"),), "k", False),
    ((("k", "", "https://KPS.example/x", "https://KPS.example", "m"),), "k", False),
    ((("k1", "", KPS + "/1", KPS, "m"), ("k2", "", KPS + "/2", KPS, "k1"), ("k3", "", KPS + "/3", KPS, "k2"),
      ("k4", "", KPS + "/4", KPS, "k3")), "k4", False),  # depth 4
    ((("k1", "", KPS + "/1", KPS, "m"), ("k2", "", KPS + "/2", KPS, "k1"), ("k3", "", KPS + "/3", KPS, "k2")),
     "k3", True),  # depth 3
    ((), "m", False),  # the top document
    ((("s", "", STRIPE + "/x", STRIPE, "m"),), "s", False),  # an allowed (reviewed) origin keeps its own path
])
def test_unreviewed_admission_is_structural_and_never_raises(frames, target, admitted):
    rows = _rows(*frames)
    assert sp._unreviewed_admitted(rows[target], rows, MERCHANT, (STRIPE,)) is admitted


# ---------------------------------------------------------------------- one-origin rule and typed ambiguity

_KPS_SET = ("cc-name", "cc-number", "cc-csc", "cc-exp-month", "cc-exp-year")


def test_one_unreviewed_document_or_five_split_documents_are_accepted():
    assert len(sp._discovery_documents([_candidate(KPS, *_KPS_SET)], MERCHANT, (STRIPE,), 1)) == 1
    split = [_candidate(KPS, t, frame=t) for t in _KPS_SET]
    assert len(sp._discovery_documents(split, MERCHANT, (STRIPE,), 5)) == 5


@pytest.mark.parametrize("candidates, error_type", [
    ([_candidate(KPS, t, frame=t) for t in _KPS_SET] + [_candidate(KPS, "postal-code", frame="zip")],
     "payment_fields_ambiguous"),  # 6 documents
    ([_candidate(KPS, "cc-number", "cc-csc", frame="a"), _candidate("https://other.example", "cc-exp", frame="b")],
     "payment_fields_ambiguous"),  # two unreviewed origins
    ([_candidate(KPS, "cc-number", "cc-csc", frame="a"), _candidate(STRIPE, "cc-exp", frame="b")],
     "payment_fields_ambiguous"),  # unreviewed plus a reviewed provider
    ([_candidate(MERCHANT, "cc-number", frame="m"), _candidate(KPS, "cc-number", "cc-csc", "cc-exp", frame="k")],
     "payment_fields_ambiguous"),  # cc-number twice
    ([_candidate(KPS, "cc-number", "cc-csc", "cc-exp", "cc-exp-month")], "payment_fields_ambiguous"),
    ([], "payment_fields_unsupported"),
    ([_candidate(KPS, "cc-number", "cc-exp")], "payment_fields_unsupported"),  # no CVC
])
def test_discovery_refusals_are_typed_and_carry_origins_and_tokens_only(candidates, error_type):
    with pytest.raises(PaymentDestinationError) as caught:
        sp._discovery_documents(candidates, MERCHANT, (STRIPE,), 3)
    exc = caught.value
    assert exc.error_type == error_type
    if error_type == "payment_fields_unsupported":
        assert exc.detail == {"unreviewed_frames_inspected": 3}  # a count: ad origins are never implied
    else:
        assert {r["origin"] for r in exc.detail["receiving_origins"]} == {c[3] for c in candidates}
        assert all(set(r) == {"origin", "reviewed", "fields"} for r in exc.detail["receiving_origins"])


# ------------------------------------------------------------------------------------- B3 traversal bounds

class _TreePage(sp.PaymentSupervisionMixin):
    def __init__(self, tree, targets=0):
        self.tree, self.targets, self.owner_probes = tree, targets, 0

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        if method == "Page.getFrameTree":
            return {"result": {"frameTree": self.tree}}
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [{"type": "iframe", "targetId": f"t{n}"} for n in range(self.targets)]}}
        if method == "DOM.getFrameOwner":  # an unrelated target (another tab): no owner in this page
            self.owner_probes += 1
            return {"error": {"message": "Frame with the given id was not found."}}
        raise AssertionError(method)


def _frame(fid, children=()):
    return {"frame": {"id": fid, "url": MERCHANT + "/" + fid, "securityOrigin": MERCHANT},
            "childFrames": list(children)}


def _chain(depth):
    node = _frame(f"d{depth}")
    for n in range(depth - 1, -1, -1):
        node = _frame(f"d{n}", [node])
    return node


@pytest.mark.parametrize("tree, too_large", [
    (_frame("top", [_frame(f"c{n}") for n in range(63)]), False),  # 64 frames
    (_frame("top", [_frame(f"c{n}") for n in range(70)]), True),  # 71 same-process frames
    (_chain(8), False),
    (_chain(9), True),
])
def test_the_frame_walk_is_bounded_while_it_walks(tree, too_large):
    page = _TreePage(tree)
    if too_large:
        with pytest.raises(PaymentDestinationError) as caught:
            asyncio.run(page._payment_tree("page"))
        assert caught.value.error_type == "payment_page_too_large"
    else:
        assert asyncio.run(page._payment_tree("page"))[0][0] in ("top", "d0")


def test_the_oopif_owner_search_is_bounded_before_it_walks():
    """Codex B r1 #9: too many iframe targets refuse before any probe; unrelated targets cost one probe per
    (target, session), not one per frame and round."""
    with pytest.raises(PaymentDestinationError) as caught:
        asyncio.run(_TreePage(_frame("top"), targets=sp._MAX_IFRAME_TARGETS + 1)._payment_tree("page"))
    assert caught.value.error_type == "payment_page_too_large"
    page = _TreePage(_frame("top", [_frame(f"c{n}") for n in range(20)]), targets=100)
    assert len(asyncio.run(page._payment_tree("page"))) == 21
    assert page.owner_probes == 100


def test_an_overflowing_or_oversized_description_is_page_too_large():
    for raw in ({"overflow": True}, [{"index": n, "label": "x" * 200} for n in range(2000)]):
        with pytest.raises(PaymentDestinationError) as caught:
            sp._bounded_description(raw)
        assert caught.value.error_type == "payment_page_too_large"
    assert sp._bounded_description([_desc(0, "cc-number")]) == [_desc(0, "cc-number")]


_MONTHS = {"type": "select", "options": [["07", "07", False]]}


@pytest.mark.parametrize("raw, error_type", [
    ([{}], "payment_page_unverifiable"),  # an empty row is no card-free document
    ([{k: v for k, v in _desc(0, "cc-number").items() if k != "index"}], "payment_page_unverifiable"),
    ([_desc(True, "cc-number")], "payment_page_unverifiable"),  # a bool is not an index
    ([_desc(3, "email"), _desc(3, "cc-number")], "payment_page_unverifiable"),  # duplicate index
    ([_desc(4, "email"), _desc(2, "cc-number")], "payment_page_unverifiable"),  # out of document order
    ([_desc(300, "cc-number")], "payment_page_unverifiable"),  # outside the element limit
    ([_desc(0, "cc-number", rendered=True)], "payment_page_unverifiable"),  # an extra key
    ([_desc(0, "cc-number", options=[])], "payment_page_unverifiable"),  # options on a text input
    ([_desc(0, "cc-exp-month", type="select")], "payment_page_unverifiable"),  # a select without its options
    ([_desc(0, "cc-number", maxLength="19")], "payment_page_unverifiable"),
    ({"unknown": True}, "payment_page_unverifiable"),  # a field whose exposure the closure cannot judge
    (None, "payment_page_unverifiable"),
    ([_desc(n, "off") for n in range(301)], "payment_page_too_large"),  # more rows than the closure may return
])
def test_a_malformed_discovery_description_is_refused_typed(raw, error_type):
    """Codex B r1 #5: validated before classification, absence scans included."""
    with pytest.raises(PaymentDestinationError) as caught:
        sp._bounded_description(raw)
    assert caught.value.error_type == error_type
    assert sp._bounded_description([_desc(0, "cc-exp-month", **_MONTHS)])  # a complete select row passes


# ---------------------------------------------------------------------------- page-side closures under Node

def _node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the page-side closures")
    run = subprocess.run([node], input=script, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# A minimal DOM: each element has a rect, computed-style overrides and a parent; html/body span the page.
_DOM = r"""
const STYLE = {visibility: 'visible', clip: 'auto', opacity: '1', overflowX: 'visible', overflowY: 'visible',
               clipPath: 'none', pointerEvents: 'auto'};
const win = {scrollX: 0, scrollY: 0, innerWidth: 1000, innerHeight: 800,
             getComputedStyle: n => Object.assign({}, STYLE, n.css || {})};
const getComputedStyle = win.getComputedStyle;
const document = {defaultView: win, forms: []};
const R = (l, t, w, h) => ({left: l, top: t, right: l + w, bottom: t + h, width: w, height: h});
const node = (rect, css, parent, extra) => Object.assign({isConnected: true, assignedSlot: null, parentNode: null,
  ownerDocument: document, css: css || {}, parentElement: parent || null, getBoundingClientRect: () => rect,
  getClientRects: () => rect.width > 0 ? [rect] : [], getRootNode: () => document}, extra || {});
const html = node(R(0, 0, 1000, 9000), {}, null, {scrollWidth: 1000, scrollHeight: 9000});
const body = node(R(0, 0, 1000, 9000), {}, html, {scrollWidth: 1000, scrollHeight: 9000});
document.documentElement = html; document.body = body;
let hit = null;
document.elementFromPoint = () => hit;
"""

_EXPOSED_HARNESS = _DOM + r"""
const EXPOSED = __EXPOSED__;
const spec = __CASE__;
const wrap = node(R(...spec.wrap), spec.wrapCss, body);
const el = node(R(...spec.rect), spec.css, wrap);
hit = spec.covered ? body : el;
process.stdout.write(JSON.stringify({exposed: EXPOSED(el)}));
"""


@pytest.mark.parametrize("case, exposed", [
    ({"rect": [100, 100, 200, 30], "wrap": [0, 0, 1000, 600]}, True),
    ({"rect": [100, 100, 200, 30], "wrap": [0, 0, 50, 50], "wrapCss": {"overflowX": "hidden", "overflowY": "hidden"}},
     False),  # clipped away by an ancestor
    ({"rect": [100, 100, 200, 30], "wrap": [0, 0, 1000, 600], "covered": True}, False),  # an overlay at its centre
    ({"rect": [-9999, 100, 200, 30], "wrap": [0, 0, 1000, 600]}, False),  # fixed offscreen
    ({"rect": [100, 100, 200, 30], "wrap": [0, 0, 1000, 600], "wrapCss": {"opacity": "0"}}, False),  # transparent
    ({"rect": [100, 100, 1, 1], "wrap": [0, 0, 1000, 600]}, False),  # 1 px honeypot
    ({"rect": [100, 100, 200, 30], "wrap": [0, 0, 1000, 600], "css": {"clip": "rect(0px, 0px, 0px, 0px)"}}, False),
    ({"rect": [100, 1500, 200, 30], "wrap": [0, 0, 1000, 2000], "covered": True}, True),  # below the fold: deferred
    ({"rect": [100, 9500, 200, 30], "wrap": [0, 0, 1000, 9900]}, False),  # below the end of the page
    # Codex B r1 #7: clip paths. A fully inset copy below the fold is hidden; a shape Hermes cannot judge is
    # unknown (null) there, and in the viewport the hit test (which honours clipping) decides.
    ({"rect": [100, 1500, 200, 30], "wrap": [0, 0, 1000, 2000], "css": {"clipPath": "inset(100%)"}}, False),
    ({"rect": [100, 1500, 200, 30], "wrap": [0, 0, 1000, 2000], "wrapCss": {"clipPath": "inset(0px 0px 100% 0px)"}},
     False),
    ({"rect": [100, 1500, 200, 30], "wrap": [0, 0, 1000, 2000], "css": {"clipPath": "polygon(0 0, 0 0, 0 0)"}},
     None),
    ({"rect": [100, 1500, 200, 30], "wrap": [0, 0, 1000, 2000], "wrapCss": {"clip": "rect(0px, 0px, 0px, 0px)"}},
     None),
    ({"rect": [100, 100, 200, 30], "wrap": [0, 0, 1000, 600], "css": {"clipPath": "polygon(0 0, 0 0, 0 0)"},
      "covered": True}, False),
    ({"rect": [100, 100, 200, 30], "wrap": [0, 0, 1000, 600], "css": {"clipPath": "inset(0px round 8px)"}}, True),
    # Codex B r2 #4: below the fold, a 10x10 copy inside the corner a rounded inset cuts away: unknown, not exposed.
    ({"rect": [2, 1502, 10, 10], "wrap": [0, 1500, 200, 200], "wrapCss": {"clipPath": "inset(0px round 100px)"}},
     None),
])
def test_exposure_predicate_table(case, exposed):
    case = {"wrapCss": {}, "css": {}, "covered": False, **case}
    script = _EXPOSED_HARNESS.replace("__EXPOSED__", sp._EXPOSED).replace("__CASE__", json.dumps(case))
    assert _node(script)["exposed"] is exposed


_INSPECT_HARNESS = _DOM + r"""
let mono = 1000;
const performance = {now: () => mono};
class HTMLInputElement { get value() { return ''; } set value(v) {} }
class HTMLSelectElement { get value() { return ''; } set value(v) {} }
class Event {} class InputEvent extends Event {} class FocusEvent extends Event {}
const location = {href: 'https://kps.example/customer/wallet/pc/WalletInput.jsp'};
const spec = __CASE__;
const shown = node(R(0, 0, 1000, 9000), {}, body);
const transparent = node(R(0, 0, 1000, 9000), spec.altCss || {opacity: '0'}, body);
const inputs = [];
spec.inputs.forEach(([token, alternate], n) => {
  const rect = R(10, (alternate && spec.altTop || 10) + n * 25, 300, 20);
  const el = node(rect, {}, alternate ? transparent : shown, {tagName: 'INPUT', autocomplete: token, name: 'f' + n,
    id: 'f' + n, labels: null, placeholder: '', type: 'text', form: null, maxLength: 0, disabled: false,
    readOnly: false, tabIndex: 0, shadowRoot: null, getAttribute: () => null, hasAttribute: () => false,
    style: {setProperty() {}}});
  Object.setPrototypeOf(el, HTMLInputElement.prototype);
  inputs.push(el);
});
document.querySelectorAll = () => inputs;
// The hit test answers with the input under the point (the visible form's inputs; the alternate form is below).
document.elementFromPoint = (x, y) => inputs.find(e => !spec.inputs[inputs.indexOf(e)][1] &&
  e.getBoundingClientRect().top <= y && y <= e.getBoundingClientRect().bottom) || body;
const api = __INSPECT__;
const d = api.describe();
const before = api.valid();
if (spec.reveal) transparent.css = {};  // page code shows the alternate form after inspection
process.stdout.write(JSON.stringify({describe: Array.isArray(d) ? d.map(x => x.index) : d, before,
                                     valid: api.valid()}));
"""


def _inspect(script_src, inputs, full=False, **spec):
    script = _INSPECT_HARNESS.replace("__INSPECT__", script_src).replace(
        "__CASE__", json.dumps({"inputs": inputs, **spec}))
    out = _node(script)
    return out if full else out["describe"]


def test_a_transparent_alternate_form_is_no_candidate_in_discovery():
    """B2: an opacity:0 copy of the card form is skipped before candidate counting, so the real form is the only
    candidate; the reviewed closure keeps today's behaviour."""
    inputs = [["cc-number", False], ["cc-csc", False], ["cc-exp", False],
              ["cc-number", True], ["cc-csc", True], ["cc-exp", True]]
    assert _inspect(sp._INSPECT_DISCOVERY, inputs) == [0, 1, 2]
    assert _inspect(sp._INSPECT, inputs) == [0, 1, 2, 3, 4, 5]


def test_revealing_a_hidden_duplicate_in_a_receiving_document_changes_it():
    """Codex B r1 #2: discovery binds the exposed set; the reviewed closure keeps today's valid()."""
    inputs = [["cc-number", False], ["cc-csc", False], ["cc-exp", False], ["cc-number", True]]
    out = _inspect(sp._INSPECT_DISCOVERY, inputs, full=True, reveal=True, altTop=1500)  # shown below the fold
    assert out["describe"] == [0, 1, 2] and out["before"] is True and out["valid"] is False
    assert _inspect(sp._INSPECT, inputs, full=True, reveal=True, altTop=1500)["valid"] is True


def test_a_field_with_an_unjudgeable_clip_makes_the_description_unknown():
    """Codex B r1 #7: a polygon-clipped copy below the fold is neither a candidate nor proven hidden."""
    inputs = [["cc-number", False], ["cc-csc", False], ["cc-exp", False], ["cc-number", True]]
    out = _inspect(sp._INSPECT_DISCOVERY, inputs, altCss={"clipPath": "polygon(0 0, 0 0, 0 0)"}, altTop=1500)
    assert out == {"unknown": True}
    assert _inspect(sp._INSPECT_DISCOVERY, inputs, altCss={"clipPath": "inset(100%)"}, altTop=1500) == [0, 1, 2]


def test_discovery_stops_at_300_fields():
    inputs = [["off", False]] * 301
    assert _inspect(sp._INSPECT_DISCOVERY, inputs) == {"overflow": True}
    assert len(_inspect(sp._INSPECT_DISCOVERY, inputs[:300])) == 300
    assert len(_inspect(sp._INSPECT, inputs)) == 301  # the reviewed path is unchanged


_WITNESS_HARNESS = r"""
const document = {body: {innerText: __TEXT__}};
const location = {href: 'https://shop.example/checkout'};
const w = (__WITNESS__)(__CENTS__, __CURRENCY__);
const before = w.valid();
const pin = w.pin();
document.body.innerText = __LATER__;
process.stdout.write(JSON.stringify({pin, before, after: w.valid()}));
"""


@pytest.mark.parametrize("text, cents, currency, state", [
    ("Total EUR 222.66; saving EUR 22.66", 2266, "EUR", "ambiguous"),
    ("-22,66 €", 2266, "EUR", "not_found"),
    ("CA$22.66", 2266, "USD", "not_found"),
    ("Gesamtbetrag 22,66 €", 2266, "EUR", "matched"),
    ("Zwischensumme 20,00 €\nGesamt 22,66 €", 2266, "EUR", "matched"),  # a subtotal is not a total keyword
    ("Order total: US$22.66", 2266, "USD", "matched"),
    ("2 Tickets 22,66\nSumme 23,00 €", 2266, "EUR", "not_found"),  # no currency next to the stated total
    ("122,66 €", 2266, "EUR", "not_found"),
    ("Gesamt 1.022,66 EUR", 102266, "EUR", "matched"),
    # Codex B r1 #6: the whole expression, sign and every unit, is read.
    ("Total -EUR 22.66", 2266, "EUR", "not_found"),
    ("Total € -22,66", 2266, "EUR", "not_found"),
    ("Total $22.66 CAD", 2266, "USD", "ambiguous"),
    ("VAT 22,66 €\nTotal 22,66 €", 2266, "EUR", "matched"),  # a word that is no currency code
    # Codex B r2 #3: no grammar. One unit per side, then 8 characters free of any other currency, sign, digit or
    # negative-marking bracket; anything else beside a total is unclear, never matched (a second amount right
    # after the total too, by its digits).
    ("Total 22,66 € (25,00 CHF)", 2266, "EUR", "ambiguous"),
    ("Total CAD $22.66", 2266, "USD", "ambiguous"),
    ("Total -EUR $22.66", 2266, "USD", "not_found"),
    ("Total 22.66 USD CAD", 2266, "USD", "ambiguous"),
    ("Total (EUR 22.66)", 2266, "EUR", "not_found"),
    ("Total (€22.66)", 2266, "EUR", "not_found"),
    ("Total 22.66\nTotal EUR 22.66", 2266, "EUR", "ambiguous"),  # a total without a unit is unclear
    ("Total: EUR 22.66 inkl. MwSt.", 2266, "EUR", "matched"),
    ("Gesamt (inkl. MwSt.) 22,66 €", 2266, "EUR", "matched"),  # a bracket that cannot mark a negative
    # Codex B r3 #3: a later total in a format not read, and a bracket opened far from the amount.
    ("Total EUR 22.66\nTotal EUR 100", 2266, "EUR", "ambiguous"),
    ("Total EUR 22.66\nTotal EUR 100.000", 2266, "EUR", "ambiguous"),
    ("Total (amount payable in EUR 22.66 including all applicable taxes)", 2266, "EUR", "not_found"),
])
def test_amount_witness_states(text, cents, currency, state):
    values = {"__TEXT__": json.dumps(text), "__LATER__": json.dumps(text), "__WITNESS__": sp._AMOUNT_WITNESS,
              "__CENTS__": str(cents), "__CURRENCY__": json.dumps(currency)}
    out = _node(re.sub("|".join(values), lambda m: values[m.group(0)], _WITNESS_HARNESS))
    assert out["pin"]["state"] == state and out["before"] is True and out["after"] is True


def test_an_unclear_labelled_total_on_any_document_blocks_matched():
    pins = [("merchant page", {"state": "matched", "labelled": [2266], "stated": 1, "unclear": False}),
            ("payment form", {"state": "ambiguous", "labelled": [], "stated": 1, "unclear": True})]
    assert sp._page_match(pins, 2266)[0] == "ambiguous"
    assert not sp._valid_pin({"state": "matched", "labelled": [2266], "stated": 1})  # unclear is required


def test_a_total_changed_after_inspection_invalidates_the_witness():
    values = {"__TEXT__": json.dumps("Gesamtbetrag 22,66 €"), "__LATER__": json.dumps("Gesamtbetrag 24,66 €"),
              "__WITNESS__": sp._AMOUNT_WITNESS, "__CENTS__": "2266", "__CURRENCY__": json.dumps("EUR")}
    out = _node(re.sub("|".join(values), lambda m: values[m.group(0)], _WITNESS_HARNESS))
    assert out["pin"]["state"] == "matched" and out["after"] is False


# ------------------------------------------------------------------------------------- B4 topology binding

_TREE = (("top", "L0", MERCHANT + "/checkout", MERCHANT, ""),
         ("ad", "L1", AD + "/slot?1", AD, "top"),
         ("kps", "L2", _KPS_URL, KPS, "top"))


class _Fresh(sp.PaymentSupervisionMixin):
    """The real _payment_is_current over a stubbed tree walk, exposure test and CDP answers."""

    def __init__(self, current, exposed=(), scan=()):
        self.current, self.exposed, self.scan, self.calls = current, set(exposed), list(scan), []
        self._state_lock, self._active, self._page_session_id = threading.Lock(), True, "page"

    async def _payment_tree(self, sid, child_sessions=None):
        return self.current

    async def _chain_exposed(self, fid, by_id, child_sessions, page_sid, cache):
        return fid in self.exposed

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        fn = (params or {}).get("functionDeclaration", "")
        self.calls.append(fn[:40])
        if "this.valid()" in fn:
            return {"result": {"result": {"value": True}}}
        if "this.scan()" in fn:
            return {"result": {"result": {"value": self.scan}}}
        if fn in (sp._OWNERS_EXPOSED, sp._OWNERS_SHOWN):
            return {"result": {"result": {"value": [True] * (len(params["arguments"]))}}}
        raise AssertionError(method)


def _unreviewed_inspection(tree=_TREE, unreviewed=(KPS,)):
    doc = PaymentDocument("kps", "s-kps", "o-kps", KPS, _controls(*_KPS_SET))
    return PaymentInspection("f" * 32, "page", "target", MERCHANT, (doc,), tree, time.monotonic() + 60,
                             frame_owners={"top": ("page", [("kps", "owner-kps")])}, unreviewed_origins=unreviewed,
                             absence=(("page", "absent-top"),))


def _with(tree, fid, **change):
    keys = ("id", "loader", "url", "origin", "parent")
    return tuple(tuple(change.get(k, v) for k, v in zip(keys, r)) if r[0] == fid else r for r in tree)


def _fresh(sup, **kwargs):
    i = _unreviewed_inspection(**kwargs)
    i.owner = sup
    return asyncio.run(sup._payment_is_current(i))


@pytest.mark.parametrize("current, exposed, scan, fresh", [
    (_with(_TREE, "ad", loader="L9", url=AD + "/slot?2"), (), (), True),  # the ad navigates: the fill proceeds
    (_TREE + (("ad2", "L5", AD + "/x", AD, "top"),), {"ad2"}, (), True),  # a new ad frame
    (_TREE + (("k2", "L5", KPS + "/again", KPS, "top"),), {"k2"}, (), False),  # a new shown frame on the approved origin
    (_TREE + (("k2", "L5", KPS + "/again", KPS, "top"),), (), (), True),  # ... hidden: not a frame the user sees
    (_TREE + (("m2", "L5", "about:blank", MERCHANT, "top"),), {"m2"}, (), False),  # a new merchant frame, shown
    (_with(_TREE, "kps", loader="L7"), (), (), False),  # the card iframe swapped
    (_with(_TREE, "top", url=MERCHANT + "/other"), (), (), False),  # the top document navigated
    (_TREE, (), [_desc(3, "cc-number")], False),  # cc-number injected into the top document
    (_TREE, (), [_desc(3, "email")], True),  # an unrelated field: still card-free
    (_TREE, (), [{}], False),  # Codex B r1 #5: a malformed scan is not a card-free document
])
def test_unreviewed_freshness_binds_the_relevant_topology_only(current, exposed, scan, fresh):
    assert _fresh(_Fresh(current, exposed, scan)) is fresh


def test_a_reviewed_inspection_keeps_exact_tree_equality_and_no_extra_calls():
    assert _fresh(_Fresh(_with(_TREE, "ad", loader="L9")), unreviewed=()) is False
    sup = _Fresh(_TREE)
    assert _fresh(sup, unreviewed=()) is True
    assert len(sup.calls) == 1  # one valid() per document; no scan, no owner pass


# ------------------------------------------------------------------------------- fill_payment, B5-B8 flows

def _kps_inspection(merchant_holder=False, page_match=("matched", "the merchant page and the payment form")):
    docs = [PaymentDocument("kps", "s-kps", "o-kps", KPS,
                            _controls(*(t for t in _KPS_SET if not (merchant_holder and t == "cc-name"))))]
    if merchant_holder:
        docs.insert(0, PaymentDocument("top", "page", "o-top", MERCHANT, _controls("cc-name")))
    return PaymentInspection("a" * 32, "page", "target", MERCHANT, tuple(docs), _TREE, time.monotonic() + 120,
                             unreviewed_origins=(KPS,), witnesses=(("page", "w-top"),), page_match=page_match)


@pytest.fixture
def flow(monkeypatch):
    """fill_payment on a managed lease: the reviewed pass refuses for coverage, the discovery pass returns a
    KPS-like inspection. Records every step; the switch is read through ``policy`` in order."""
    events, state = [], {"policy": [], "consent": "accept", "reason": "accepted", "cart_current": True,
                         "guard": (True, ""), "inspection": _kps_inspection(), "reviewed": None}

    class Supervisor:
        def inspect_payment(self, merchant, *, bound_origins=(), frame_origins=(), unreviewed=False, cart=None):
            events.append(("inspect", "discovery" if unreviewed else "reviewed", cart))
            if not unreviewed:
                if state["reviewed"] is not None:
                    raise state["reviewed"]
                raise PaymentPolicyError("Missing, ambiguous or split payment documents")
            if state.get("discovery") is not None:
                raise state["discovery"]
            state["inspection"].owner = self
            return state["inspection"]

        def payment_is_current(self, i):
            events.append("current")
            return True

        def payment_cart_current(self, i):
            events.append("cart_current")
            return state["cart_current"]

        def commit_payment(self, i, fills, strict=False):
            events.append(("commit", strict, sorted((f["document"], f["token"]) for f in fills)))
            i.consumed = True
            return {"success": True, "filled_fields": len(fills), "retention_verified": True}

        def discard_payment(self, i):
            events.append("discard")

    sup = Supervisor()
    consent_calls = []

    def consent(message, description, **kwargs):
        consent_calls.append(kwargs)
        events.append("consent")
        kwargs["outcome"]["reason"] = state["reason"]
        return state["consent"]

    def policy():
        events.append("policy")
        return state["policy"].pop(0) if state["policy"] else (True, "g1")

    def resolve(handle):
        events.append("resolve")
        return dict(CARD)

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: sup)
    monkeypatch.setattr("tools.browser_vault_tool._managed_supervisor", lambda task_id, admit=False: sup)
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: True)
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: {"lease": "l1", "token": "t1"})
    monkeypatch.setattr("tools.browser_control_route.set_capture_guard", lambda l, t: state["guard"])
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda *a, **k: events.append("mark"))
    monkeypatch.setattr("tools.browser_payment_surface.claim_payment_surface", lambda s, i: events.append("claim"))
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", consent)
    monkeypatch.setattr(bvp, "destination_policy", policy)
    monkeypatch.setattr("agent.redact.register_vault_redaction_value", lambda v: None)
    meta = SimpleNamespace(id="h", kind="payment", label="Synthetic Card", allowed_origins=(), origin=None,
                           payment_scope="confirmed_page", payment_frame_origins=())
    backend = SimpleNamespace(name="local", get_meta=lambda h: meta, resolve_secret=resolve)

    def run(**kwargs):
        raw = bvp.fill_payment(backend, meta, "task", **kwargs)
        assert not any(m in raw for m in MARKERS)
        return json.loads(raw)

    run.state, run.consent_calls = state, consent_calls
    return run, events


def _inspects(events):
    return [e[1] for e in events if isinstance(e, tuple) and e[0] == "inspect"]


def test_switch_off_keeps_the_reviewed_refusal_with_a_hint_and_no_discovery(flow):
    run, events = flow
    run.state["policy"] = [(False, "g0")]
    out = run(cart=CART, approve_origins=[KPS])
    assert out["error_type"] == "payment_policy_refused" and "vault.payment_destination_consent" in out["error"]
    assert _inspects(events) == ["reviewed"] and "consent" not in events and "resolve" not in events


def test_without_cart_and_origins_discovery_reports_destinations_and_reads_nothing(flow):
    run, events = flow
    raw_out = run()
    assert raw_out["error_type"] == "payment_destination_approval_required"
    assert raw_out["needs"] == ["cart", "approve_origins"] and raw_out["approval_origins"] == [KPS]
    assert raw_out["receiving"] == [{"origin": KPS, "reviewed": False, "fields": sorted(_KPS_SET)}]
    text = json.dumps(raw_out)
    assert "/customer" not in text and "?" not in text.replace(raw_out["error"], "") and "jsp" not in text
    assert _inspects(events) == ["reviewed", "discovery"] and "consent" not in events and "resolve" not in events
    assert events[-1] == "discard"


def test_an_invalid_cart_is_a_need_not_a_guess(flow):
    run, events = flow
    bad = dict(CART, total="22,66")
    out = run(cart=bad, approve_origins=[KPS])
    assert out["error_type"] == "payment_destination_approval_required" and out["needs"] == ["cart"]
    assert ("inspect", "discovery", None) in events  # no witness for a cart that is not valid
    assert "consent" not in events


@pytest.mark.parametrize("holder, approve, error_type", [
    (False, ["https://kps-paymnet.example"], "payment_destination_changed"),  # origin B for page A
    (True, [KPS], "payment_destination_changed"),  # B7: the merchant holder field is a receiving origin too
    (True, [MERCHANT, KPS], None),
    (False, [KPS, KPS], "payment_destination_approval_required"),  # duplicates are malformed
])
def test_approve_origins_must_equal_the_one_approval_origin_set(flow, holder, approve, error_type):
    run, events = flow
    run.state["inspection"] = _kps_inspection(merchant_holder=holder)
    out = run(cart=CART, approve_origins=approve)
    if error_type is None:
        assert out["success"] is True and out["approval_origins"] == [MERCHANT, KPS]
        receiving = run.consent_calls[0]["payment"]["receiving"]
        assert [(r["origin"], r["status"]) for r in receiving] == [(MERCHANT, "merchant"), (KPS, "UNREVIEWED")]
    else:
        assert out["error_type"] == error_type and "consent" not in events and "resolve" not in events


def test_the_owner_card_names_the_unreviewed_destination_and_the_fill_runs_strict(flow):
    from tools.payment_consent_prompt import PaymentPromptFacts, render_html

    run, events = flow
    out = run(cart=CART, approve_origins=[KPS])
    assert out["success"] is True and out["destination_reviewed"] is False and out["submitted"] is False
    assert out["page_match"] == "matched" and out["cart_stated_by"] == "sera"
    kwargs = run.consent_calls[0]
    assert kwargs["surface"] == "vault-payment" and 1 <= kwargs["timeout_seconds"] <= 120
    facts = kwargs["payment"]
    assert facts["unreviewed"] is True and facts["ref"] == "a" * 32
    assert facts["receiving"] == [{"origin": KPS, "status": "UNREVIEWED", "fields": list(_KPS_SET)}]
    assert facts["cart"] == {"items": CART["items"], "total": "22.66", "currency": "EUR"}
    assert facts["page_match"]["state"] == "matched"
    card = render_html(PaymentPromptFacts.from_dict(facts), budget_utf16=4096)
    assert "UNREVIEWED" in card and "stated by Sera" in card and KPS in card
    assert ("inspect", "discovery", (2266, "EUR")) in events
    order = [e if isinstance(e, str) else e[0] for e in events if e != "policy"]
    assert order == ["inspect", "inspect", "consent", "current", "cart_current", "mark", "claim", "resolve",
                     "commit"]
    commit = next(e for e in events if isinstance(e, tuple) and e[0] == "commit")
    assert commit[1] is True  # strict: per-setter hit tests, guard without the rollout exception
    # captured at inspection, after accept, in context, after the page checks, at the commit fence
    assert events.count("policy") == 5


def test_amount_not_found_still_prompts_with_the_warning(flow):
    """D2: no total found on the page is shown to the owner, not refused."""
    from tools.payment_consent_prompt import PaymentPromptFacts, render_html

    run, events = flow
    run.state["inspection"] = _kps_inspection(page_match=("not_found", "the merchant page and the payment form"))
    run.state["consent"], run.state["reason"] = "decline", "refused"
    out = run(cart=CART, approve_origins=[KPS])
    assert out["error_type"] == "payment_declined" and "resolve" not in events
    card = render_html(PaymentPromptFacts.from_dict(run.consent_calls[0]["payment"]), budget_utf16=4096)
    assert "NOT FOUND" in card and "3-D Secure" not in card


@pytest.mark.parametrize("verdict, reason, error_type", [
    ("decline", "refused", "payment_declined"), ("cancel", "timeout", "payment_consent_missing"),
    ("decline", "unavailable", "payment_consent_missing"),
])
def test_no_approval_means_no_read_and_no_write(flow, verdict, reason, error_type):
    run, events = flow
    run.state["consent"], run.state["reason"] = verdict, reason
    out = run(cart=CART, approve_origins=[KPS])
    assert out["error_type"] == error_type
    assert "resolve" not in events and not any(isinstance(e, tuple) and e[0] == "commit" for e in events)


@pytest.mark.parametrize("off_at, read", [(1, False), (2, False), (3, False), (4, True)])
def test_switching_off_withdraws_a_fill_in_progress(flow, off_at, read):
    """B6: off (or a changed policy generation) after the accept, in context, after the page checks (Codex B r1:
    those are asynchronous), or at the commit fence."""
    run, events = flow
    run.state["policy"] = [(True, "g1")] * off_at + [(False, "g2")]
    out = run(cart=CART, approve_origins=[KPS])
    assert out["error_type"] == "payment_destination_disabled"
    assert ("resolve" in events) is read
    assert not any(isinstance(e, tuple) and e[0] == "commit" for e in events)
    assert ("no field received it" in out["error"]) is read


def test_a_changed_policy_generation_also_withdraws(flow):
    run, events = flow
    run.state["policy"] = [(True, "g1"), (True, "g2")]
    assert run(cart=CART, approve_origins=[KPS])["error_type"] == "payment_destination_disabled"
    assert "resolve" not in events


def test_a_changed_total_stops_before_the_card_is_read(flow):
    run, events = flow
    run.state["cart_current"] = False
    out = run(cart=CART, approve_origins=[KPS])
    assert out["error_type"] == "payment_cart_changed" and "resolve" not in events


def test_unreviewed_fills_ignore_the_capture_guard_rollout_exception(flow, monkeypatch):
    from tools import browser_control_route as route

    run, events = flow
    monkeypatch.setenv(route.CAPTURE_GUARD_OPTIONAL_ENV, "1")
    run.state["guard"] = (False, "control_without_capture_guard")
    out = run(cart=CART, approve_origins=[KPS])
    assert out["error_type"] == "payment_capture_guard_unavailable" and "resolve" not in events


def test_an_unmanaged_browser_is_refused_before_discovery(flow, monkeypatch):
    run, events = flow
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: False)
    out = run(cart=CART, approve_origins=[KPS])
    assert out["error_type"] == "payment_destination_requires_managed_browser"
    assert _inspects(events) == ["reviewed"]


def test_discovery_without_an_unreviewed_destination_returns_the_reviewed_refusal(flow):
    run, events = flow
    reviewed_only = _kps_inspection()
    reviewed_only.unreviewed_origins = ()
    run.state["inspection"] = reviewed_only
    out = run()
    assert out["error_type"] == "payment_policy_refused" and "consent" not in events and events[-1] == "discard"


@pytest.mark.parametrize("reviewed, error_type", [
    (PaymentDestinationError("payment_page_too_large", "fixed text"), "payment_page_too_large"),
    (PaymentPolicyError("Merchant origin mismatch"), "payment_policy_refused"),
])
def test_only_a_coverage_refusal_leads_to_discovery(flow, reviewed, error_type):
    run, events = flow
    run.state["reviewed"] = reviewed
    assert run(cart=CART, approve_origins=[KPS])["error_type"] == error_type
    assert _inspects(events) == ["reviewed"]


def test_no_card_value_reaches_a_result_or_the_log(flow, caplog):
    run, _events = flow
    caplog.set_level(logging.DEBUG)
    outs = [run(cart=CART, approve_origins=[KPS])]
    run.state["policy"] = [(True, "g1")] * 4 + [(False, "g2")]  # read, then withdrawn at the commit fence
    outs.append(run(cart=CART, approve_origins=[KPS]))
    assert outs[0]["success"] is True and outs[1]["error_type"] == "payment_destination_disabled"
    blob = json.dumps(outs) + caplog.text
    assert not any(m in blob for m in MARKERS)


def test_list_reports_the_destination_switch(monkeypatch):
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(vault, "destination_consent_enabled", lambda: True)
    meta = SimpleNamespace(id="h", kind="payment", label="Synthetic Card", origin=None, allowed_origins=(),
                           payment_scope="confirmed_page", payment_frame_origins=(), has_otp=False, identifier="")
    backend = SimpleNamespace(name="local", display_name="Local", needs_unlock=False, list_items=lambda: [meta])
    monkeypatch.setattr("agent.vault_backends.enabled_backends", lambda: [backend])
    entry = json.loads(vault.browser_vault_list())["items"][0]
    assert entry["destination_consent"] == "per_fill_owner_approval"
    assert KPS not in entry["hosted_origins"]  # the reviewed set stays truthful


# ------------------------------------------------------------------ the discovery inspection over a fake CDP

class _DiscoveryPage(sp.PaymentSupervisionMixin):
    """A KPS-like checkout: frames, which owner <iframe>s are exposed, and what each document describes."""

    def __init__(self, frames, exposed, controls):
        self.frames, self.exposed, self.controls, self.evaluated = frames, set(exposed), controls, []
        self._state_lock, self._active, self._page_session_id = threading.Lock(), True, "page"

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        if method == "Target.getTargetInfo":
            return {"result": {"targetInfo": {"targetId": "target"}}}
        if method == "Target.getTargets":
            return {"result": {"targetInfos": []}}
        if method == "Page.getFrameTree":
            def node(fid):
                rec = next(r for r in self.frames if r[0] == fid)
                return {"frame": {"id": fid, "loaderId": rec[1], "url": rec[2], "securityOrigin": rec[3]},
                        "childFrames": [node(r[0]) for r in self.frames if r[4] == fid]}
            return {"result": {"frameTree": node(self.frames[0][0])}}
        if method == "Page.createIsolatedWorld":
            return {"result": {"executionContextId": "ctx-" + params["frameId"]}}
        if method == "DOM.getFrameOwner":
            return {"result": {"backendNodeId": params["frameId"]}}
        if method == "DOM.resolveNode":
            return {"result": {"object": {"objectId": "owner-" + params["backendNodeId"]}}}
        if method == "Runtime.evaluate":
            fid = params["contextId"][4:]
            kind = "w" if "(cents, currency) =>" in params["expression"] else "doc"
            self.evaluated.append((kind, fid, params["expression"] is sp._INSPECT_DISCOVERY))
            return {"result": {"result": {"objectId": f"{kind}-{fid}"}}}
        if method == "Runtime.callFunctionOn":
            obj, fn = params["objectId"], params["functionDeclaration"]
            if fn in (sp._OWNERS_EXPOSED, sp._OWNERS_SHOWN):
                return {"result": {"result": {"value": [a["objectId"][6:] in self.exposed
                                                        for a in params["arguments"]]}}}
            if obj.startswith("owner-"):
                return {"result": {"result": {"value": obj[6:] in self.exposed}}}
            if "describe()" in fn:
                return {"result": {"result": {"value": self.controls.get(obj[4:], [])}}}
            if "pin()" in fn:
                return {"result": {"result": {"value": {"state": "matched", "labelled": [2266], "stated": 1,
                                                        "unclear": False}}}}
        if method in ("Runtime.releaseObject", "Target.detachFromTarget"):
            self.released = getattr(self, "released", []) + [params.get("objectId")]
            return {"result": {}}
        raise AssertionError((method, params))


_CARD_FORM = [_desc(n, t) for n, t in enumerate(_KPS_SET)]
_PAGE = (("top", "L0", MERCHANT + "/checkout", MERCHANT, ""),
         ("ad", "L1", AD + "/slot", AD, "top"),
         ("kps", "L2", _KPS_URL, KPS, "top"),
         ("copy", "L3", KPS + "/customer/wallet/pc/WalletInput.jsp?copy", KPS, "top"))


def test_discovery_admits_the_exposed_unreviewed_form_and_pins_the_rest():
    page = _DiscoveryPage(_PAGE, exposed={"kps", "ad"}, controls={"kps": _CARD_FORM, "copy": _CARD_FORM})
    i = asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True, cart=(2266, "EUR")))
    assert [d.frame_id for d in i.documents] == ["kps"] and i.unreviewed_origins == (KPS,)
    assert i.absence == (("page", "doc-top"),)  # the card-free merchant document stays pinned
    assert [fid for kind, fid, _ in page.evaluated if kind == "w"] == ["top", "kps"]
    assert i.page_match == ("matched", "the merchant page and the payment form")
    # The hidden copy was never inspected; the ad frame was (structure only, v1 §3.2), then released: frames on
    # other origins never become witnesses. Every inspected document got the bounded discovery closure.
    assert [(fid, discovery) for kind, fid, discovery in page.evaluated if kind == "doc"] == [
        ("top", True), ("ad", True), ("kps", True)]
    assert "doc-ad" in page.released and "doc-top" not in page.released


def test_the_reviewed_pass_never_inspects_an_unreviewed_frame():
    page = _DiscoveryPage(_PAGE, exposed={"kps", "ad", "copy"}, controls={"kps": _CARD_FORM})
    with pytest.raises(PaymentPolicyError, match="Missing, ambiguous or split payment documents"):
        asyncio.run(page._inspect_payment("", (), (STRIPE,)))
    assert [fid for _kind, fid, _ in page.evaluated] == ["top"]


def test_two_exposed_copies_are_ambiguous_and_nine_unreviewed_frames_exceed_the_bound():
    page = _DiscoveryPage(_PAGE, exposed={"kps", "copy"}, controls={"kps": _CARD_FORM, "copy": _CARD_FORM})
    with pytest.raises(PaymentDestinationError) as caught:
        asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True))
    assert caught.value.error_type == "payment_fields_ambiguous"
    many = _PAGE[:1] + tuple((f"k{n}", "", f"{KPS}/f{n}", KPS, "top") for n in range(9))
    page = _DiscoveryPage(many, exposed={f"k{n}" for n in range(9)}, controls={})
    with pytest.raises(PaymentDestinationError) as caught:
        asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True))
    assert caught.value.error_type == "payment_fields_ambiguous"


def test_a_hidden_frame_revealed_after_inspection_withdraws_the_approval():
    """Codex B r1 #2: a frame present but hidden at inspection is not exempt from the exposure check; a bound
    absence witness (unchanged) is."""
    tree = _TREE + (("copy", "L3", KPS + "/copy", KPS, "top"), ("m1", "L4", MERCHANT + "/m1", MERCHANT, "top"))
    sup = _Fresh(tree, exposed={"copy"})
    assert _fresh(sup, tree=tree) is False
    i = _unreviewed_inspection(tree=tree)
    i.owner, i.absence_frames = _Fresh(tree, exposed={"m1"}), ("m1",)
    assert asyncio.run(i.owner._payment_is_current(i)) is True


def test_discovery_with_an_unknown_owner_exposure_refuses_typed():
    """Codex B r1 #4: a malformed DOM.getFrameOwner answer is unknown, never hidden: discovery refuses."""
    class Unknown(_DiscoveryPage):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "DOM.getFrameOwner" and params["frameId"] == "copy":
                return {"result": {}}
            return await super()._cdp(method, params, session_id, timeout)

    page = Unknown(_PAGE, exposed={"kps", "ad"}, controls={"kps": _CARD_FORM, "copy": _CARD_FORM})
    with pytest.raises(PaymentDestinationError) as caught:
        asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True))
    assert caught.value.error_type == "payment_page_unverifiable"


@pytest.mark.parametrize("answer, outcome", [(True, True), (False, False), (None, "raises"), ("yes", "raises")])
def test_strict_owner_check_reads_only_a_literal_boolean(answer, outcome):
    class Owner(sp.PaymentSupervisionMixin):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            return {"DOM.getFrameOwner": {"result": {"backendNodeId": 1}},
                    "Page.createIsolatedWorld": {"result": {"executionContextId": 2}},
                    "DOM.resolveNode": {"result": {"object": {"objectId": "o"}}},
                    "Runtime.callFunctionOn": {"result": {"result": {"value": answer}}},
                    "Runtime.releaseObject": {"result": {}}}[method]

    check = Owner()._owner_check("f", "top", "page", "function(){}", strict=True)
    if outcome == "raises":
        with pytest.raises(PaymentDestinationError):
            asyncio.run(check)
    else:
        assert asyncio.run(check) is outcome
    assert asyncio.run(Owner()._owner_check("f", "top", "page", "function(){}")) is (answer is True)  # reviewed


# ------------------------------------------------------------------- Codex B r1 #1: bindings during the fill

class _Filling(sp.PaymentSupervisionMixin):
    """The real _commit_payment_once over stubbed bindings and a closure that pauses after each focus."""

    def __init__(self, cart_after, policy_after=None):
        self.cart_calls, self.writes, self.cart_after, self.policy_after = 0, [], cart_after, policy_after
        self.pending: dict = {}

    async def _payment_is_current(self, i, during_fill=False):
        return True

    async def _cart_current(self, i):
        self.cart_calls += 1
        return self.cart_calls <= self.cart_after  # the first call is the commit's own freshness pass

    async def _payment_scope_current(self, i, receiving):
        return True

    async def _owner_chain_current(self, i, d, point):
        return True

    async def _shown_documents(self, i):
        return {d.frame_id for d in i.documents}

    async def _payment_retention(self, i, d):
        return [{"token": f["token"], "state": "held", "length": 1} for f in self.pending.pop(d.object_id)]

    async def _top_navigated(self, i):
        return False

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        obj, fn = params["objectId"], params["functionDeclaration"]
        if "this.commit(" in fn:
            self.pending[obj] = list(params["arguments"][0]["value"])
            self.queue = list(self.pending[obj])
            self.done = 0
        elif self.queue:  # resume(): the setter of the field focused before the pause
            self.writes.append(self.queue.pop(0)["token"])
            self.done += 1
        if self.queue:
            return {"result": {"result": {"value": {"pending": True, "filled_fields": self.done, "x": 1, "y": 1}}}}
        return {"result": {"result": {"value": {"success": True, "filled_fields": self.done}}}}


def _two_document_fill(sup, policy=None):
    docs = (PaymentDocument("top", "page", "o-top", MERCHANT, _controls("cc-name"), clock_anchor=time.monotonic()),
            PaymentDocument("kps", "s-kps", "o-kps", KPS, _controls("cc-number", "cc-csc", "cc-exp"),
                            clock_anchor=time.monotonic()))
    i = PaymentInspection("b" * 32, "page", "target", MERCHANT, docs, _TREE, time.monotonic() + 60, owner=sup,
                          unreviewed_origins=(KPS,), witnesses=(("page", "w-top"),), policy_check=policy)
    fills = [{"document": 0, "index": 0, "token": "cc-name", "value": "x"}] + [
        {"document": 1, "index": n, "token": t, "value": "x"} for n, t in enumerate(("cc-csc", "cc-exp", "cc-number"))]
    return asyncio.run(sup._commit_payment_once(i, fills, None, strict=True))


@pytest.mark.parametrize("cart_after, writes, filled", [
    (99, ["cc-name", "cc-csc", "cc-exp", "cc-number"], 4),  # the cart holds: every field is written
    (1, [], 0),  # the total changes on the first focus (merchant holder field): nothing written
    (2, ["cc-name"], 1),  # ... after the merchant document, before the hosted form: partial, reported
    (4, ["cc-name", "cc-csc"], 2),  # ... on a later focus inside the hosted form
])
def test_an_unreviewed_fill_rebinds_the_cart_between_documents_and_after_each_focus(cart_after, writes, filled):
    sup = _Filling(cart_after)
    out = _two_document_fill(sup)
    assert sup.writes == writes
    if filled == 4:
        assert out["success"] is True
    else:
        assert out["error_type"] == "payment_cart_changed" and out.get("filled_fields", 0) == filled
        assert out.get("partially_filled", False) is (filled > 0)


def test_switching_off_part_way_stops_the_fill_and_says_what_was_written():
    reads = iter([True, True, True, False])  # the commit's pass, the first focus, between documents, then off
    sup = _Filling(99)
    out = _two_document_fill(sup, policy=lambda: next(reads))
    assert out["error_type"] == "payment_destination_disabled" and out["filled_fields"] == 1
    assert sup.writes == ["cc-name"]
    bvp._commit_text(out)
    assert "some card fields were written" in out["error"]
    cart = {"success": False, "error_type": "payment_cart_changed", "filled_fields": 1, "partially_filled": True}
    bvp._commit_text(cart)
    assert "some card fields were written" in cart["error"]


def test_typed_unsupported_result_carries_no_page_path(flow):
    """Codex B r1 #8: the reviewed pass's hint keeps registry origins and fixed text in a typed result."""
    paypal = "https://www.paypal.com"
    tree = (("top", "", MERCHANT + "/checkout", MERCHANT, ""),
            ("p", "", paypal + "/payment-ref-ABCsecret", paypal, "top"))
    with pytest.raises(PaymentPolicyError) as reviewed:
        sp._explained_card_documents([], MERCHANT, tree, (paypal,))
    assert "ABCsecret" in str(reviewed.value)  # the untyped reviewed refusal is unchanged
    run, _events = flow
    run.state["reviewed"] = reviewed.value
    run.state["discovery"] = PaymentDestinationError("payment_fields_unsupported", sp.FIELDS_UNSUPPORTED,
                                                     {"unreviewed_frames_inspected": 0})
    out = run(cart=CART, approve_origins=[KPS])
    assert out["error_type"] == "payment_fields_unsupported"
    assert paypal in out["reviewed_refusal"] and "ABCsecret" not in json.dumps(out)


# ----------------------------------------------------------------------------------------------- Codex B r2

class _WrittenDocumentChanges(_Filling):
    """The real during-fill freshness over the stubbed tree: the merchant document's input handler injects a card
    field into it once its holder field is written (its valid() turns false)."""

    _payment_is_current = sp.PaymentSupervisionMixin._payment_is_current

    def __init__(self):
        super().__init__(cart_after=99)
        self._state_lock, self._active, self._page_session_id = threading.Lock(), True, "page"

    async def _payment_tree(self, sid, child_sessions=None):
        return _TREE

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        if "this.valid()" in params["functionDeclaration"]:
            changed = params["objectId"] == "o-top" and "cc-name" in self.writes
            return {"result": {"result": {"value": not changed}}}
        return await super()._cdp(method, params, session_id, timeout)


def test_an_already_written_document_that_changes_stops_the_fill():
    """Codex B r2 #1: every receiving document's valid() runs during the fill, those already written included."""
    sup = _WrittenDocumentChanges()
    out = _two_document_fill(sup)
    assert sup.writes == ["cc-name"]
    assert out == {"success": False, "error_type": "payment_page_changed", "filled_fields": 1, "partially_filled": True}


class _Replies(sp.PaymentSupervisionMixin):
    """One document's commit over scripted closure replies; an exception is a lost reply."""

    def __init__(self, replies):
        self.replies = list(replies)

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return {"result": {"result": {"value": reply}}}


def _pending(n):
    return {"pending": True, "filled_fields": n, "x": 1, "y": 1}


@pytest.mark.parametrize("replies, paced, error_type, filled", [
    ([_pending(0), _pending(1), RuntimeError("lost")], True, "payment_transport_uncertain", 1),
    ([_pending(0), _pending(1), {"pending": True, "filled_fields": 1}], True, "payment_result_invalid", 1),  # no x/y
    ([_pending(0), _pending(1), _pending(0)], True, "payment_result_invalid", 1),  # the count went backwards
    ([_pending(0), _pending(1), _pending(2), {"success": True, "filled_fields": "3"}], True,
     "payment_result_invalid", 2),  # a malformed final answer is no success
    ([RuntimeError("lost")], False, "payment_transport_uncertain", 0),  # unpaced: the commit call ran the setters
])
def test_a_lost_or_malformed_reply_after_a_write_reports_what_may_have_been_written(replies, paced, error_type, filled):
    """Codex B r2 #2: the last acknowledged count survives, and a write whose outcome is unknown is never reported
    as nothing written."""
    tokens = ("cc-csc", "cc-exp", "cc-number")
    doc = PaymentDocument("kps", "s-kps", "o-kps", KPS, _controls(*tokens))
    fills = [{"index": n, "token": t, "value": "x"} for n, t in enumerate(tokens)]

    async def check(point):
        return True

    out = asyncio.run(_Replies(replies)._commit_payment_document(doc, fills, 1000.0, strict=True,
                                                                 paced=check if paced else None))
    assert out == {"success": False, "error_type": error_type, "filled_fields": filled, "partially_filled": True,
                   "write_uncertain": True}
    bvp._commit_text(out)
    assert "may hold values" in out["error"]


@pytest.mark.parametrize("failure", [RuntimeError("target detached"), TimeoutError(),
                                     asyncio.CancelledError()])
def test_a_strict_owner_check_that_fails_in_transport_is_unverifiable(failure):
    """Codex B r2 #5: a CDP error or timeout is unknown, typed; cancellation propagates; the owner is released."""
    released = []

    class Owner(sp.PaymentSupervisionMixin):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "Runtime.callFunctionOn":
                raise failure
            if method == "Runtime.releaseObject":
                released.append(params["objectId"])
            return {"DOM.getFrameOwner": {"result": {"backendNodeId": 1}},
                    "Page.createIsolatedWorld": {"result": {"executionContextId": 2}},
                    "DOM.resolveNode": {"result": {"object": {"objectId": "o"}}},
                    "Runtime.releaseObject": {"result": {}}}[method]

    cancelled = isinstance(failure, asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError if cancelled else PaymentDestinationError) as caught:
        asyncio.run(Owner()._owner_check("f", "top", "page", "function(){}", strict=True))
    assert cancelled or caught.value.error_type == "payment_page_unverifiable"
    assert released == ["o"]


def test_a_failed_describe_releases_the_object_it_pinned():
    """Codex B r2 #6: the closure is released even when its first describe() throws."""
    class Failing(_DiscoveryPage):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "Runtime.callFunctionOn" and params["objectId"] == "doc-kps" \
                    and "describe()" in params["functionDeclaration"]:
                return {"result": {"exceptionDetails": {"text": "page threw"}}}
            return await super()._cdp(method, params, session_id, timeout)

    page = Failing(_PAGE, exposed={"kps", "ad"}, controls={"kps": _CARD_FORM})
    with pytest.raises(PaymentPolicyError):
        asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True))
    assert "doc-kps" in page.released


def test_clock_anchors_are_kept_per_frame_session(monkeypatch):
    """Codex B r2 #7: object ids are session-scoped; two card documents whose sessions hand out the same id keep
    their own clock anchors (a later anchor would shorten the earlier document's renderer deadline)."""
    tree = (("top", "L0", MERCHANT + "/checkout", MERCHANT, ""), ("kps", "L2", _KPS_URL, KPS, "top"))

    class SameIds(_DiscoveryPage):
        async def _payment_tree(self, sid, child_sessions=None):
            out = await super()._payment_tree(sid, child_sessions)
            child_sessions["kps"] = "s-kps"  # an out-of-process card frame
            return out

        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "Runtime.evaluate":
                return {"result": {"result": {"objectId": "1"}}}
            if method == "Runtime.callFunctionOn" and "describe()" in params["functionDeclaration"]:
                tokens = ("cc-name",) if session_id == "page" else _KPS_SET[1:]
                return {"result": {"result": {"value": [_desc(n, t) for n, t in enumerate(tokens)]}}}
            return await super()._cdp(method, params, session_id, timeout)

    clock = iter(range(1000))  # every monotonic read is a later tick
    monkeypatch.setattr(sp, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    i = asyncio.run(SameIds(tree, exposed={"kps"}, controls={})._inspect_payment("", (), (STRIPE,), unreviewed=True))
    top, kps = i.documents
    assert (top.frame_session, kps.frame_session) == ("page", "s-kps") and top.object_id == kps.object_id
    assert top.clock_anchor < kps.clock_anchor


# ----------------------------------------------------------------------------------------------- Codex B r3

_UNCERTAIN = {"success": False, "partially_filled": True, "write_uncertain": True}


@pytest.mark.parametrize("replies, paced, expected", [
    # An answer that counts a write but does not flag it partial is normalised: never "no card field was written".
    ([{"success": False, "error_type": "payment_fields_changed", "filled_fields": 1}], False,
     {"success": False, "error_type": "payment_fields_changed", "filled_fields": 1, "partially_filled": True}),
    ([{"success": False, "error_type": "payment_fields_changed", "filled_fields": 1, "partially_filled": False}], False,
     {"success": False, "error_type": "payment_fields_changed", "filled_fields": 1, "partially_filled": True}),
    # A success that does not count every field, a count past the fills, a failure without its type, a count that
    # went backwards: invalid, with what may have been written.
    ([_pending(0), _pending(1), {"success": True, "filled_fields": 0}], True,
     {**_UNCERTAIN, "error_type": "payment_result_invalid", "filled_fields": 1}),
    ([{"success": True}], False, {**_UNCERTAIN, "error_type": "payment_result_invalid", "filled_fields": 0}),
    ([{"success": False, "filled_fields": 9}], False,
     {**_UNCERTAIN, "error_type": "payment_result_invalid", "filled_fields": 0}),
    ([{"success": False, "filled_fields": 0}], False,
     {**_UNCERTAIN, "error_type": "payment_result_invalid", "filled_fields": 0}),
    ([_pending(0), _pending(2), {"success": False, "error_type": "payment_fields_changed", "filled_fields": 1}], True,
     {**_UNCERTAIN, "error_type": "payment_result_invalid", "filled_fields": 2}),
    # Codex B r3 #2: a focus point that is not two finite numbers stops before any setter, in every document.
    ([{"pending": True, "filled_fields": 0, "x": None, "y": "unknown"}, {"success": True, "filled_fields": 3}], True,
     {**_UNCERTAIN, "error_type": "payment_result_invalid", "filled_fields": 0}),
    ([{"pending": True, "filled_fields": 0, "x": True, "y": 1}, {"success": True, "filled_fields": 3}], True,
     {**_UNCERTAIN, "error_type": "payment_result_invalid", "filled_fields": 0}),
])
def test_final_answers_are_checked_strictly_and_never_report_a_write_away(replies, paced, expected):
    """Codex B r3 #1/#2."""
    tokens = ("cc-csc", "cc-exp", "cc-number")
    doc = PaymentDocument("top", "page", "o-top", MERCHANT, _controls(*tokens))
    fills = [{"index": n, "token": t, "value": "x"} for n, t in enumerate(tokens)]

    async def check(point):
        return True

    out = asyncio.run(_Replies(replies)._commit_payment_document(doc, fills, 1000.0, strict=True,
                                                                 paced=check if paced else None))
    assert out == expected
    bvp._commit_text(out)
    assert "no card field was written" not in out["error"]


def test_a_cancelled_inspection_releases_every_closure_it_pinned():
    """Codex B r3 #4: cancelled while a document's describe() is in flight, the inspection releases that closure and
    the earlier ones, then propagates the cancellation."""
    class Cancelled(_DiscoveryPage):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "Runtime.callFunctionOn" and params["objectId"] == "doc-kps" \
                    and "describe()" in params["functionDeclaration"]:
                raise asyncio.CancelledError
            return await super()._cdp(method, params, session_id, timeout)

    page = Cancelled(_PAGE, exposed={"kps", "ad"}, controls={"kps": _CARD_FORM})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True))
    assert {"doc-top", "doc-ad", "doc-kps"} <= set(page.released)
