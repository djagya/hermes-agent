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
    def __init__(self, tree):
        self.tree = tree

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        if method == "Page.getFrameTree":
            return {"result": {"frameTree": self.tree}}
        if method == "Target.getTargets":
            return {"result": {"targetInfos": []}}
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


def test_an_overflowing_or_oversized_description_is_page_too_large():
    for raw in ({"overflow": True}, [{"index": n, "label": "x" * 200} for n in range(2000)]):
        with pytest.raises(PaymentDestinationError) as caught:
            sp._bounded_description(raw)
        assert caught.value.error_type == "payment_page_too_large"
    assert sp._bounded_description([{"index": 0}]) == [{"index": 0}]


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
const transparent = node(R(0, 0, 1000, 9000), {opacity: '0'}, body);
const inputs = [];
spec.inputs.forEach(([token, alternate], n) => {
  const rect = R(10, 10 + n * 25, 300, 20);
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
process.stdout.write(JSON.stringify({describe: Array.isArray(d) ? d.map(x => x.index) : d}));
"""


def _inspect(script_src, inputs):
    script = _INSPECT_HARNESS.replace("__INSPECT__", script_src).replace("__CASE__", json.dumps({"inputs": inputs}))
    return _node(script)["describe"]


def test_a_transparent_alternate_form_is_no_candidate_in_discovery():
    """B2: an opacity:0 copy of the card form is skipped before candidate counting, so the real form is the only
    candidate; the reviewed closure keeps today's behaviour."""
    inputs = [["cc-number", False], ["cc-csc", False], ["cc-exp", False],
              ["cc-number", True], ["cc-csc", True], ["cc-exp", True]]
    assert _inspect(sp._INSPECT_DISCOVERY, inputs) == [0, 1, 2]
    assert _inspect(sp._INSPECT, inputs) == [0, 1, 2, 3, 4, 5]


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
])
def test_amount_witness_states(text, cents, currency, state):
    values = {"__TEXT__": json.dumps(text), "__LATER__": json.dumps(text), "__WITNESS__": sp._AMOUNT_WITNESS,
              "__CENTS__": str(cents), "__CURRENCY__": json.dumps(currency)}
    out = _node(re.sub("|".join(values), lambda m: values[m.group(0)], _WITNESS_HARNESS))
    assert out["pin"]["state"] == state and out["before"] is True and out["after"] is True


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
    (_TREE, (), [{"index": 3, "autocomplete": "cc-number"}], False),  # cc-number injected into the top document
    (_TREE, (), [{"index": 3, "autocomplete": "email"}], True),  # an unrelated field: still card-free
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
    assert events.count("policy") == 4  # captured at inspection, after accept, in context, at the commit fence


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


@pytest.mark.parametrize("off_at, read", [(1, False), (2, False), (3, True)])
def test_switching_off_withdraws_a_fill_in_progress(flow, off_at, read):
    """B6: off (or a changed policy generation) after the accept, before the card is read, or at the commit fence."""
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
    run.state["policy"] = [(True, "g1")] * 3 + [(False, "g2")]  # read, then withdrawn at the commit fence
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
                return {"result": {"result": {"value": {"state": "matched", "labelled": [2266], "stated": 1}}}}
        if method in ("Runtime.releaseObject", "Target.detachFromTarget"):
            self.released = getattr(self, "released", []) + [params.get("objectId")]
            return {"result": {}}
        raise AssertionError((method, params))


_CARD_FORM = [{"index": n, "autocomplete": t} for n, t in enumerate(_KPS_SET)]
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
