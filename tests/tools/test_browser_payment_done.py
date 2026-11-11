"""browser_vault payment_done (lockdown design §2): wait for a safe confirmation document on the native
connection, snapshot it without input values, open a new tab before closing the payment tab, and report the
lock released only when control's status proves it. Fake supervisor and fake CDP only; synthetic data."""

import asyncio
import json
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from tools import browser_payment_done as done
from tools import browser_payment_surface as surface

SHOP = "https://shop.test"
STRIPE = "https://js.stripe.com"
CARD_URL = STRIPE + "/v3/elements-inner-card-1a.html"
CHECKOUT = (("TOP", "L0", SHOP + "/checkout", SHOP, ""), ("CARD", "LC", CARD_URL, STRIPE, "TOP"))
ACS = (("TOP", "L1", "https://acs.bank.test/challenge", "https://acs.bank.test", ""),)  # 3-D Secure redirect
PSP_TOP = (("TOP", "L2", "https://pay.datatrans.com/upp/start", "https://pay.datatrans.com", ""),)
CONFIRM = (("TOP", "L3", SHOP + "/thanks?order=77", SHOP, ""),
           # Stripe.js's hidden controller frame stays on many shop pages: it holds no card field
           ("CTRL", "LX", STRIPE + "/v3/controller-9f.html", STRIPE, "TOP"))
UNLOCKED = {"locked": False, "native": False, "tabs": [], "unverified": False}
SNAPSHOT = {"title": "Thank you", "url": SHOP + "/thanks?order=77", "origin": SHOP, "ready": "complete",
            "text": "Order 77 confirmed", "text_truncated": False,
            "links": [{"url": SHOP + "/tickets/77.pdf", "text": "Tickets"}], "links_truncated": False,
            "card_fields": 0}


class FakeSup:
    """The supervisor surface payment_done touches, on a real loop thread; trees served in order."""

    def __init__(self, trees, status=None):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._state_lock = threading.Lock()
        self._page_session_id, self._active = "S-page", True
        self.trees, self.tree = list(trees), None
        self.snapshot = dict(SNAPSHOT)
        self.status = status if status is not None else dict(UNLOCKED)
        self.calls, self.fail = [], set()
        self.tab_open = True

    async def _cdp(self, method, params=None, *, session_id=None, timeout=10.0):
        self.calls.append((method, params or {}, session_id, self.tree))
        if method in self.fail:
            raise RuntimeError(f"CDP error on id=1: {method} failed")
        return {"id": len(self.calls), "result": self.answer(method, params or {})}

    def answer(self, method, params):
        if method == "Target.getTargets":
            tabs = [{"targetId": "TAB", "type": "page", "url": (self.tree or CHECKOUT)[0][2], "title": "Shop",
                     "browserContextId": "CTX"}] if self.tab_open else []
            return {"targetInfos": tabs + [{"targetId": "OTHER", "type": "page", "url": "https://x.test/"}]}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "TAB"}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            if params["expression"] != done.SNAPSHOT_JS:  # the payment_controls lister
                return {"result": {"objectId": "arr"}}
            return {"result": {"type": "object", "value": self.snapshot}}
        if method == "Runtime.callFunctionOn":
            fn = params["functionDeclaration"]
            if fn == surface._DESCRIBE:
                return {"result": {"value": [{"i": 0, "role": "button", "text": "Pay 22.66 EUR", "disabled": False}]}}
            return {"result": {"value": True}} if fn == surface._HIT else {"result": {"objectId": "el"}}
        if method == "DOM.getContentQuads":
            return {"quads": [[10, 10, 30, 10, 30, 30, 10, 30]]}
        if method == "Target.createTarget":
            return {"targetId": "NEWTAB"}
        if method == "Hermes.paymentSurfaceStatus":
            return self.status
        return {}

    async def _payment_tree(self, sid, child_sessions):
        if self.trees:
            self.tree = self.trees.pop(0)
        return self.tree

    async def _release_payment_sessions(self, sessions):
        pass

    async def _release_payment_object(self, sid, obj):
        pass

    async def _enable_page_domains(self, sid, timeout=5):
        pass

    async def _attach_page(self, target_id, *, timeout, deadline=None):
        return "S-new"

    def sent(self, method):
        return [c for c in self.calls if c[0] == method]

    def index(self, method):
        return next(i for i, c in enumerate(self.calls) if c[0] == method)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(done, "_POLL_S", 0.0)
    monkeypatch.setattr(done, "_WAIT_S", 0.4)
    monkeypatch.setattr(done, "_RELEASE_WAIT_S", 0.2)
    monkeypatch.setattr(done, "_RELEASE_POLL_S", 0.01)


def _paid(trees, merchant_fields=False, **kw):
    sup = FakeSup(trees, **kw)
    docs = (SimpleNamespace(frame_id="CARD", origin=STRIPE),)
    tree = CHECKOUT
    if merchant_fields == "subframe":  # the holder name in a same-origin merchant frame, since removed
        tree = CHECKOUT + (("MSUB", "LM", SHOP + "/holder", SHOP, "TOP"),)
        docs += (SimpleNamespace(frame_id="MSUB", origin=SHOP),)
    elif merchant_fields:  # e.g. the holder name on the merchant page of a split checkout
        docs += (SimpleNamespace(frame_id="TOP", origin=SHOP),)
    done.record_payment(sup, SimpleNamespace(page_id="TAB", merchant_origin=SHOP, tree=tree, documents=docs,
                                             unreviewed_origins=()))
    return sup


def test_waits_through_3ds_and_provider_pages_then_hands_over():
    sup = _paid([CHECKOUT, ACS, PSP_TOP, CONFIRM])
    out = done.payment_done(sup)
    assert out["success"] is True and out["lock_released"] is True and out["payment_tab_closed"] is True
    assert (out["url"], out["title"], out["text"]) == (SHOP + "/thanks?order=77", "Thank you", "Order 77 confirmed")
    assert out["links"] == SNAPSHOT["links"] and out["new_tab"] == "NEWTAB" and "note" not in out
    # the snapshot was read only from the confirmation, never from the checkout, 3-D Secure or provider page
    assert {c[3] for c in sup.sent("Runtime.evaluate")} == {CONFIRM}
    # the new tab is opened (same browser context, same URL) before the payment tab is closed
    (_, created, _, _), = sup.sent("Target.createTarget")
    assert created == {"url": SHOP + "/thanks?order=77", "browserContextId": "CTX"}
    assert sup.index("Target.createTarget") < sup.index("Target.closeTarget")
    assert [c[1] for c in sup.sent("Target.closeTarget")] == [{"targetId": "TAB"}]
    assert done.payment_done(sup)["error_type"] == "payment_done_no_payment"  # handed over once


def test_an_interstitial_seen_once_is_never_snapshotted():
    # the shop's "processing" page is safe but replaced at once: only a document seen twice is read
    processing = (("TOP", "L9", SHOP + "/processing", SHOP, ""),)
    sup = _paid([processing, CONFIRM])
    assert done.payment_done(sup)["success"] is True
    assert {c[3] for c in sup.sent("Runtime.evaluate")} == {CONFIRM}


CARD_AGAIN =(CONFIRM[0], ("CARD2", "LZ", CARD_URL, STRIPE, "TOP"))  # a card element at this payment's path
OLD_CARD = (CONFIRM[0], CHECKOUT[1])  # this payment's own card document still in the tree
SAME_DOC = ((("TOP", "L0", SHOP + "/thanks", SHOP, ""),))  # a single-page shop: the checkout document itself
SAME_DOC_CARD = (SAME_DOC[0], ("CARD", "LC2", STRIPE + "/v3/loading.html", STRIPE, "TOP"))  # claimed frame, reloaded


@pytest.mark.parametrize("tree, snapshot", [
    (ACS, None),                         # 3-D Secure never finishes
    (PSP_TOP, None),                     # top document on a payment provider
    (CARD_AGAIN, None),
    (OLD_CARD, None),
    (SAME_DOC_CARD, None),
    (CONFIRM, {"card_fields": 1}),       # card inputs on the page
    (CONFIRM, {"ready": "loading"}),
    (CONFIRM, {"links": [{"url": "javascript:alert(1)", "text": "x"}]}),
    (CONFIRM, {"links": [{"url": SHOP + "/a", "text": "a"}] * (done.MAX_LINKS + 1)}),
    (CONFIRM, {"origin": "https://other.test"}),
    (CONFIRM, {"extra": "field"}),
])
def test_no_handover_until_a_safe_confirmation_and_the_lock_stays(tree, snapshot):
    sup = _paid([CHECKOUT, tree])
    if snapshot:
        sup.snapshot.update(snapshot)
    out = done.payment_done(sup)
    assert out["error_type"] == "payment_confirmation_not_reached" and out["success"] is False
    assert out["lock_released"] is False and set(out) == {"success", "error_type", "error", "url", "title",
                                                          "lock_released"}
    # still locked: the tab's site only, never its title or path (exfil via document.title / location)
    assert out["url"] == done._url_origin(tree[0][2]) and out["title"] == ""
    assert not sup.sent("Target.createTarget") and not sup.sent("Target.closeTarget")
    if snapshot is None:  # an unsafe document is never read at all
        assert not sup.sent("Runtime.evaluate") and not sup.sent("Page.createIsolatedWorld")


@pytest.mark.parametrize("status, released", [
    (UNLOCKED, True),
    ({"locked": True, "native": True, "tabs": ["TAB"], "unverified": False}, False),
    ({"locked": True, "native": True, "tabs": ["OTHER"], "unverified": False}, False),  # another payment holds it
    ({"locked": True, "native": False, "tabs": [], "unverified": True}, False),  # human clear only
    ({"locked": False, "native": False, "tabs": ["TAB"], "unverified": False}, False),  # contradictory
    ({"locked": False, "native": True, "tabs": [], "unverified": False}, False),
    ({"locked": False, "native": False, "tabs": [], "unverified": True}, False),
    ({"locked": "false", "native": False, "tabs": [], "unverified": False}, False),
    ({"locked": 0, "native": False, "tabs": [], "unverified": False}, False),
    ({"locked": False, "tabs": []}, False),         # a missing key
    ({**UNLOCKED, "extra": True}, False),           # an unknown key
    ({"protected": False}, False),                  # the pre-lockdown shape proves nothing
    ({**UNLOCKED, "tabs": [{"id": "TAB"}]}, False),
    (None, False),
    (RuntimeError("socket reset"), False),
])
def test_lock_released_only_when_status_proves_it(status, released):
    sup = _paid([CONFIRM])
    if isinstance(status, Exception):
        sup.fail.add("Hermes.paymentSurfaceStatus")
    else:
        sup.status = status
    out = done.payment_done(sup)
    assert out["success"] is True and out["payment_tab_closed"] is True and out["lock_released"] is released
    assert ("note" in out) is (not released)


def test_single_page_shop_confirmation_once_the_card_frames_are_gone():
    sup = _paid([CHECKOUT, SAME_DOC])
    sup.snapshot.update(url=SHOP + "/thanks")
    out = done.payment_done(sup)
    assert out["success"] is True and out["url"] == SHOP + "/thanks"
    assert {c[3] for c in sup.sent("Runtime.evaluate")} == {SAME_DOC}
    # the same document never counts when the merchant page itself received a card field
    for merchant_fields in (True, "subframe"):
        sup = _paid([CHECKOUT, SAME_DOC], merchant_fields=merchant_fields)
        sup.snapshot.update(url=SHOP + "/thanks")
        assert done.payment_done(sup)["error_type"] == "payment_confirmation_not_reached"
        assert not sup.sent("Runtime.evaluate")


def test_new_tab_failure_never_closes_the_payment_tab():
    sup = _paid([CONFIRM])
    sup.fail.add("Target.createTarget")
    out = done.payment_done(sup)
    assert out["success"] is True and out["new_tab"] is None and out["lock_released"] is False
    assert out["text"] == "Order 77 confirmed" and "note" in out
    assert not sup.sent("Target.closeTarget")


@pytest.mark.parametrize("origin", ["http://shop.test", "https://shop.test/", STRIPE, "https://pay.datatrans.com",
                                    "shop.test"])
def test_expected_origin_must_be_a_canonical_shop_origin(origin):
    sup = _paid([CONFIRM])
    assert done.payment_done(sup, origin)["error_type"] == "payment_done_origin_invalid"
    assert not sup.calls


def test_confirmation_on_the_named_origin():
    other = "https://tickets.example.test"
    page = ((("TOP", "L3", other + "/order/77", other, ""),))
    sup = _paid([page])
    sup.snapshot.update(url=other + "/order/77", origin=other)
    assert done.payment_done(sup)["error_type"] == "payment_confirmation_not_reached"
    sup = _paid([page])
    sup.snapshot.update(url=other + "/order/77", origin=other)
    assert done.payment_done(sup, other)["success"] is True


def test_tab_gone_is_typed_and_checks_the_lock():
    sup = _paid([CONFIRM])
    sup.tab_open = False
    out = done.payment_done(sup)
    assert out["error_type"] == "payment_tab_gone" and out["lock_released"] is True
    assert not sup.sent("Runtime.evaluate") and not sup.sent("Target.createTarget")


_SNAPSHOT_HARNESS = r"""
const reads = [];
const T = (data) => ({nodeType: 3, data});
const E = (tag, kids = [], o = {}) => Object.assign({nodeType: 1, tagName: tag, childNodes: kids, attrs: {},
  style: {display: 'block', visibility: 'visible'}, getAttribute(n) { return this.attrs[n] ?? null; }}, o);
const field = (tag, attrs, kids = []) => {
  const el = E(tag, kids, {attrs});
  Object.defineProperty(el, 'value', {get() { reads.push(tag); return '4000000000000002'; }});
  return el;
};
const card = field('INPUT', {autocomplete: 'cc-number'});
const plain = field('INPUT', {name: 'email'});
const area = field('TEXTAREA', {}, [T('4111 1111 1111 1111')]);
const select = field('SELECT', {}, [E('OPTION', [T('12/2030')])]);
const anchors = [
  E('A', [T('Your tickets')], {href: 'https://shop.test/tickets/77.pdf'}),
  E('A', [T('Run')], {href: 'javascript:alert(1)'}),
  E('A', [T('Inline')], {href: 'data:text/html,hi'}),
  E('A', [T('Mail')], {href: 'mailto:help@shop.test'}),
  E('A', [], {href: 'https://shop.test/account', attrs: {'aria-label': 'Account'}}),
];
for (let i = 0; i < 250; i++) anchors.push(E('A', [T('Item ' + i)], {href: 'https://shop.test/i/' + i}));
const host = E('DIV', [T('light text ')], {shadowRoot: {nodeType: 11, childNodes: [T('Shadow total 22.66')]}});
const body = E('BODY', [
  E('H1', [T('Thank you,  order 77')]),
  E('FORM', [E('LABEL', [T('Card')]), card, plain, area, select, E('BUTTON', [T('Print')])]),
  E('DIV', [T('4242 4242 4242 4242 typed')], {isContentEditable: true}),
  E('DIV', [T('hidden 5555 5555 5555 4444')], {style: {display: 'none', visibility: 'visible'}}),
  E('SCRIPT', [T('var secret = "378282246310005"')]),
  E('IFRAME', [T('frame fallback')]),
  host,
  ...anchors.slice(0, 2),
]);
const document = {title: ' Order   confirmed ', readyState: 'complete', body,
  querySelectorAll: (sel) => sel.includes('a[href]') ? anchors : [card, plain, area, select]};
const location = {href: 'https://shop.test/thanks?order=77', origin: 'https://shop.test'};
const getComputedStyle = (el) => el.style;
const out = SNAPSHOT;
process.stdout.write(JSON.stringify({out, reads}));
"""


def test_snapshot_script_never_reads_values_and_bounds_links():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic page")
    harness = _SNAPSHOT_HARNESS.replace("SNAPSHOT", done.SNAPSHOT_JS)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    out, dumped = got["out"], run.stdout
    assert got["reads"] == []  # no .value of any control was touched
    for synthetic in ("4000000000000002", "4111 1111", "12/2030", "4242 4242", "5555", "378282246310005",
                      "frame fallback"):
        assert synthetic not in dumped
    assert out["title"] == "Order confirmed" and out["card_fields"] == 1
    assert "Thank you, order 77" in out["text"] and "Print" in out["text"] and "Shadow total 22.66" in out["text"]
    urls = [link["url"] for link in out["links"]]
    assert len(urls) == done.MAX_LINKS and out["links_truncated"] is True
    assert all(u.startswith(("https://", "http://")) for u in urls)
    assert out["links"][:2] == [{"url": "https://shop.test/tickets/77.pdf", "text": "Your tickets"},
                                {"url": "https://shop.test/account", "text": "Account"}]


def _top_controls(sup):
    return surface.payment_controls(sup)  # the fake serves the next tree to _payment_tree


def _top_ref(tree):
    """The ref the payment tab's top document gets (bound to its loader), as opposed to a card document's."""
    return surface._ref(f"{tree[0][0]}|{tree[0][1]}", 0, "button", "Pay 22.66 EUR")


@pytest.mark.parametrize("tree", [CHECKOUT, ACS])  # the merchant checkout, a 3-D Secure redirect page
def test_locked_payment_tab_top_document_is_listed_and_clicked_natively(tree):
    sup = _paid([tree])
    out = _top_controls(sup)
    assert out["success"] is True
    assert {"ref": _top_ref(tree), "role": "button", "text": "Pay 22.66 EUR", "disabled": False} in out["controls"]
    worlds = [c for c in sup.sent("Page.createIsolatedWorld") if c[1]["frameId"] == "TOP"]
    assert len(worlds) == 1 and worlds[0][2] == "S-page"
    assert surface.payment_click(sup, _top_ref(tree))["success"] is True
    assert [c[1]["type"] for c in sup.sent("Input.dispatchMouseEvent")] == [
        "mouseMoved", "mousePressed", "mouseReleased"]
    assert {c[2] for c in sup.sent("Input.dispatchMouseEvent")} == {"S-page"}


@pytest.mark.parametrize("tree, merchant_fields, recorded", [
    (PSP_TOP, False, True),            # a payment provider's own page
    (CHECKOUT, True, True),            # the merchant document that received card fields: a card document only
    (CHECKOUT, False, False),          # no payment recorded for this connection
])
def test_top_document_never_offered_when_it_is_a_card_frame(tree, merchant_fields, recorded):
    sup = _paid([tree], merchant_fields=merchant_fields) if recorded else FakeSup([tree])
    out = _top_controls(sup)
    assert _top_ref(tree) not in [c["ref"] for c in out.get("controls", [])]
    if not merchant_fields:  # nothing at all is read on a provider page or without a recorded payment
        assert not sup.sent("Page.createIsolatedWorld")
    assert not sup.sent("Input.dispatchMouseEvent")


def test_a_top_document_ref_dies_with_its_document():
    sup = _paid([CHECKOUT])
    ref = _top_ref(CHECKOUT)
    assert ref in [c["ref"] for c in _top_controls(sup)["controls"]]
    sup.trees = [(("TOP", "L5", SHOP + "/checkout", SHOP, ""),)]  # navigated: same frame and text, new document
    assert surface.payment_click(sup, ref)["error_type"] == "payment_control_unknown"
    assert not sup.sent("Input.dispatchMouseEvent")
