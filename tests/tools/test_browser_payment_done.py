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


def frame_tree(rows):
    """``Page.getFrameTree``'s result for tree rows ``(id, loader, url, origin, parent)``."""
    def node(row):
        return {"frame": {"id": row[0], "loaderId": row[1], "url": row[2]},
                "childFrames": [node(r) for r in rows if r[4] == row[0]]}
    return {"frameTree": node(rows[0])}


class FakeSup:
    """The supervisor surface payment_done touches, on a real loop thread; trees served in order."""

    def __init__(self, trees, status=None):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._state_lock = threading.Lock()
        self._page_session_id, self._active = "S-page", True
        self.trees, self.tree = list(trees), None
        self.page = dict(SNAPSHOT)
        self.status = status if status is not None else dict(UNLOCKED)
        self.calls, self.fail = [], set()
        self.tab_open = True
        self.dialogs, self.extra_tabs, self.closing = [], [], []
        self.targets = {}  # session id → target id; the page session is "TAB", others "OTHER"

    async def _cdp(self, method, params=None, *, session_id=None, timeout=10.0):
        self.calls.append((method, params or {}, session_id, self.tree))
        if method in self.fail:
            raise RuntimeError(f"CDP error on id=1: {method} failed")
        return {"id": len(self.calls), "result": self.answer(method, params or {}, session_id)}

    def answer(self, method, params, session_id=None):
        if method == "Target.getTargets":
            tabs = [{"targetId": "TAB", "type": "page", "url": (self.tree or CHECKOUT)[0][2], "title": "Shop",
                     "browserContextId": "CTX"}] if self.tab_open else []
            return {"targetInfos": tabs + self.extra_tabs + [{"targetId": "OTHER", "type": "page",
                                                              "url": "https://x.test/"}]}
        if method == "Target.getTargetInfo":
            target = self.targets.get(session_id or "",
                                      "TAB" if session_id == self._page_session_id else "OTHER")
            return {"targetInfo": {"targetId": target, "url": (self.tree or CHECKOUT)[0][2]}}
        if method == "Target.attachToTarget":
            return {"sessionId": "S-temp"}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            if params["expression"] != done.SNAPSHOT_JS:  # the payment_controls lister
                return {"result": {"objectId": "arr"}}
            return {"result": {"type": "object", "value": self.page}}
        if method == "Runtime.callFunctionOn":
            fn = params["functionDeclaration"]
            if fn == surface._DESCRIBE:
                return {"result": {"value": [{"i": 0, "role": "button", "text": "Pay 22.66 EUR", "disabled": False}]}}
            return {"result": {"value": True}} if fn == surface._HIT else {"result": {"objectId": "el"}}
        if method == "DOM.getContentQuads":
            return {"quads": [[10, 10, 30, 10, 30, 30, 10, 30]]}
        if method == "Page.getFrameTree":
            return frame_tree(self.tree)
        if method == "Target.createTarget":
            return {"targetId": "NEWTAB"}
        if method == "Target.closeTarget":
            self.closing.append(getattr(self, "_payment_handover_closing", None))
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
        self.calls.append(("attach_page", {"targetId": target_id}, None, self.tree))
        return "S-new"

    def sent(self, method):
        return [c for c in self.calls if c[0] == method]

    def snapshot(self):
        return SimpleNamespace(pending_dialogs=list(self.dialogs))

    def respond_to_dialog(self, action, *, prompt_text=None, dialog_id=None, timeout=10.0):
        found = [d for d in self.dialogs if d.id == dialog_id]
        self.calls.append(("respond_to_dialog", {"action": action, "dialog_id": dialog_id}, None, self.tree))
        if not found:
            return {"ok": False, "error": "no dialog"}
        self.dialogs.remove(found[0])
        return {"ok": True, "dialog": {"id": dialog_id}}

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
    # Codex P2: a replacement card frame on a default provider at another path, and any other provider frame
    ((CONFIRM[0], ("CARD2", "LZ", STRIPE + "/v3/elements-inner-cvc-77.html", STRIPE, "TOP")), None),
    ((CONFIRM[0], ("DT", "LD", "https://pay.datatrans.com/upp/payment/x", "https://pay.datatrans.com", "TOP")), None),
    ((CONFIRM[0], ("FAKE", "LF", STRIPE + "/v3/controller-x.html/../card.html", STRIPE, "TOP")), None),
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
        sup.page.update(snapshot)
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
    # following the new tab installs the dialog bridge: only once the slot is proven unlocked
    assert bool(sup.sent("attach_page")) is released


def test_single_page_shop_confirmation_once_the_card_frames_are_gone():
    sup = _paid([CHECKOUT, SAME_DOC])
    sup.page.update(url=SHOP + "/thanks")
    out = done.payment_done(sup)
    assert out["success"] is True and out["url"] == SHOP + "/thanks"
    assert {c[3] for c in sup.sent("Runtime.evaluate")} == {SAME_DOC}
    # the same document never counts when the merchant page itself received a card field
    for merchant_fields in (True, "subframe"):
        sup = _paid([CHECKOUT, SAME_DOC], merchant_fields=merchant_fields)
        sup.page.update(url=SHOP + "/thanks")
        assert done.payment_done(sup)["error_type"] == "payment_confirmation_not_reached"
        assert not sup.sent("Runtime.evaluate")


def test_new_tab_failure_never_closes_the_payment_tab():
    sup = _paid([CONFIRM])
    sup.fail.add("Target.createTarget")
    out = done.payment_done(sup)
    assert out["success"] is True and out["new_tab"] is None and out["lock_released"] is False
    assert out["text"] == "Order 77 confirmed" and "note" in out
    assert not sup.sent("Target.closeTarget")


HPP = "https://payments.worldpay.com"  # a provider's own hosted payment page as the checkout tab
TICKETS = "https://tickets.example.test"
TICKET_PAGE = (("TOP", "L3", TICKETS + "/order/77", TICKETS, ""),)


def _paid_on_provider_page(trees):
    sup = FakeSup(trees)
    tree = (("TOP", "L0", HPP + "/app/hpp/pay", HPP, ""),)
    done.record_payment(sup, SimpleNamespace(page_id="TAB", merchant_origin=HPP, tree=tree, unreviewed_origins=(),
                                             documents=(SimpleNamespace(frame_id="TOP", origin=HPP),)))
    sup.page.update(url=TICKETS + "/order/77", origin=TICKETS)
    return sup


@pytest.mark.parametrize("origin", ["", "http://tickets.example.test", "https://tickets.example.test/", STRIPE,
                                    "https://pay.datatrans.com", HPP, "tickets.example.test"])
def test_expected_origin_is_required_and_strict_on_a_provider_page_checkout(origin):
    sup = _paid_on_provider_page([TICKET_PAGE])
    assert done.payment_done(sup, origin)["error_type"] == "payment_done_origin_invalid"
    assert [c[0] for c in sup.calls] == ["Target.getTargets"]  # which tabs are still open; nothing read
    sup = _paid_on_provider_page([TICKET_PAGE])
    assert done.payment_done(sup, TICKETS)["success"] is True


@pytest.mark.parametrize("origin", [TICKETS, "https://acs.bank.test", "not an origin"])
def test_expected_origin_is_ignored_on_a_shop_checkout_and_says_so(origin):
    # a stable bank (3-D Secure) page must never qualify because the agent named its origin
    sup = _paid([ACS])
    out = done.payment_done(sup, origin)
    assert out["error_type"] == "payment_confirmation_not_reached" and out["expected_origin_ignored"] is True
    assert not sup.sent("Runtime.evaluate")
    sup = _paid([CONFIRM])
    out = done.payment_done(sup, origin)
    assert out["success"] is True and out["expected_origin_ignored"] is True and "ignored" in out["expected_origin_note"]
    assert "expected_origin_ignored" not in done.payment_done(_paid([CONFIRM]))


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


def _top_ref(sup, tree):
    """The ref the latest listing gave the payment tab's top document (its key: frame id + loader), or None."""
    with surface._LOCK:
        issued = dict(surface._ISSUED.get(sup) or {})
    refs = [ref for ref, spec in issued.items() if spec[0] == f"{tree[0][0]}|{tree[0][1]}"]
    return refs[0] if refs else None


@pytest.mark.parametrize("tree", [CHECKOUT, ACS])  # the merchant checkout, a 3-D Secure redirect page
def test_locked_payment_tab_top_document_is_listed_and_clicked_natively(tree):
    sup = _paid([tree])
    # r7 P1: captions run through the dialog redactor; the one amount they may show is the approved total
    done.record_approved_amount(sup, "TAB", "22.66", "EUR")
    out = _top_controls(sup)
    assert out["success"] is True
    ref = _top_ref(sup, tree)
    assert {"ref": ref, "role": "button", "text": "Pay 22.66 EUR", "disabled": False} in out["controls"]
    worlds = [c for c in sup.sent("Page.createIsolatedWorld") if c[1]["frameId"] == "TOP"]
    assert len(worlds) == 1 and worlds[0][2] == "S-page"
    assert surface.payment_click(sup, ref)["success"] is True
    assert [c[1]["type"] for c in sup.sent("Input.dispatchMouseEvent")] == [
        "mouseMoved", "mousePressed", "mouseReleased"]
    assert {c[2] for c in sup.sent("Input.dispatchMouseEvent")} == {"S-page"}


def test_a_caption_amount_stays_masked_without_an_approved_total():
    # r7 P1: every amount but an owner-approved total is masked in a caption, like in dialog text
    sup = _paid([CHECKOUT])
    assert _top_controls(sup)["controls"][0]["text"] == "Pay ••.•• EUR"


@pytest.mark.parametrize("tree, merchant_fields, recorded", [
    (PSP_TOP, False, True),            # a payment provider's own page
    (CHECKOUT, True, True),            # the merchant document that received card fields: a card document only
    (CHECKOUT, False, False),          # no payment recorded for this connection
])
def test_top_document_never_offered_when_it_is_a_card_frame(tree, merchant_fields, recorded):
    sup = _paid([tree], merchant_fields=merchant_fields) if recorded else FakeSup([tree])
    _top_controls(sup)
    wide = [c for c in sup.sent("Runtime.evaluate") if c[1]["expression"] == surface._TOP_LISTER]
    if merchant_fields:  # listed as the card document it is (keyed by its loader), with the top's wide bound
        assert len(wide) == 1 and _top_ref(sup, tree) is not None
    else:
        assert not wide and _top_ref(sup, tree) is None
    if not merchant_fields:  # nothing at all is read on a provider page or without a recorded payment
        assert not sup.sent("Page.createIsolatedWorld")
    assert not sup.sent("Input.dispatchMouseEvent")


def test_a_top_document_ref_dies_with_its_document():
    sup = _paid([CHECKOUT])
    _top_controls(sup)
    ref = _top_ref(sup, CHECKOUT)
    assert ref is not None
    sup.trees = [(("TOP", "L5", SHOP + "/checkout", SHOP, ""),)]  # navigated: same frame and text, new document
    assert surface.payment_click(sup, ref)["error_type"] == "payment_control_unknown"
    assert not sup.sent("Input.dispatchMouseEvent")


_SCAN_HARNESS = r"""
const T = (data) => ({nodeType: 3, data});
const E = (tag, kids = [], o = {}) => Object.assign({nodeType: 1, tagName: tag, childNodes: kids, attrs: {},
  style: {display: 'block', visibility: 'visible'}, getAttribute(n) { return this.attrs[n] ?? null; }}, o);
const all = (root) => root.childNodes.flatMap((n) => n.nodeType === 1 ? [n, ...all(n)] : []);
const shadow = (kids) => { const r = {nodeType: 11, childNodes: kids}; r.querySelectorAll = () => all(r); return r; };
const card = E('INPUT', [], {attrs: {autocomplete: 'cc-number'}});
const inner = E('DIV', [], {shadowRoot: shadow([E('SPAN', [T('Card')]), card])});
const host = E('PAYMENT-ELEMENT', [], {shadowRoot: shadow([inner])});  // nested open shadow roots
const body = E('BODY', [E('H1', [T('Thanks')]), host]);
const document = {title: 'x', readyState: 'complete', body,
  querySelectorAll: (sel) => sel.includes('a[href]') ? [] : all(body)};
const location = {href: 'https://shop.test/thanks', origin: 'https://shop.test'};
const getComputedStyle = (el) => el.style;
process.stdout.write(JSON.stringify(SNAPSHOT));
"""


@pytest.mark.parametrize("bound, expected", [(None, 1), (3, -1)])
def test_card_scan_reaches_open_shadow_roots_and_refuses_past_its_bound(bound, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic page")
    script = done.SNAPSHOT_JS if bound is None else done.SNAPSHOT_JS.replace(f"> {done.MAX_SCAN})", f"> {bound})")
    assert bound is None or script != done.SNAPSHOT_JS
    run = subprocess.run([node], input=_SCAN_HARNESS.replace("SNAPSHOT", script), text=True, capture_output=True,
                         timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["card_fields"] == expected  # 1: found inside two shadow roots; -1: scan bound hit
    assert done._valid_snapshot(out, "https://shop.test") is None  # either way: never a safe document


def _confirm(text, opened=1.0, kind="confirm", did="d-1", url=SHOP + "/checkout", session="S-page", frame="TOP"):
    return SimpleNamespace(id=did, type=kind, message=text, opened_at=opened, url=url, cdp_session_id=session,
                           frame_id=frame)


def test_a_held_confirm_is_reported_masked_and_answered_once():
    sup = _paid([CHECKOUT])
    sup.dialogs = [_confirm("Place  order 4111111111111111 for 22.66 EUR? " + "x" * 300),
                   _confirm("Leave?", opened=0.5, kind="alert", did="d-0")]  # an alert is never held
    out = done.payment_done(sup)
    held = out["dialog_pending"]
    # returned at once (no 120 s wait), nothing read or closed
    assert out["error_type"] == "payment_dialog_pending" and out["lock_released"] is False
    assert not sup.sent("Runtime.evaluate") and not sup.sent("Target.closeTarget")
    assert (held["id"], held["type"], held["origin"], held["on_payment_tab_top"], held["on_shop_page"]) == (
        "d-1", "confirm", SHOP, True, True)
    assert (held["text"].startswith("Place order •••••••••••••••• for ••.•• EUR?")
            and len(held["text"]) == 200)
    assert "4111" not in json.dumps(out)
    assert surface.payment_controls(sup)["dialog_pending"] == held
    assert done.payment_dialog(sup, "d-1", "true")["error_type"] == "payment_dialog_invalid"
    assert done.payment_dialog(sup, "", True)["error_type"] == "payment_dialog_invalid"  # the id is required
    assert done.payment_dialog(sup, "d-9", True)["error_type"] == "payment_dialog_none"  # not that dialog
    assert done.payment_dialog(sup, "d-1", True) == {"success": True, "answered": "accept", "type": "confirm",
                                                     "id": "d-1"}
    assert [c[1] for c in sup.sent("respond_to_dialog")] == [{"action": "accept", "dialog_id": "d-1"}]
    assert done.payment_dialog(sup, "d-1", True)["error_type"] == "payment_dialog_none"  # one answer per dialog
    assert "dialog_pending" not in surface.payment_controls(sup)
    assert done.payment_dialog(FakeSup([CONFIRM]), "d-1", False)["error_type"] == "payment_dialog_none"  # no payment


@pytest.mark.parametrize("dialog, error", [
    (_confirm("Pay?", url=STRIPE + "/v3/frame", frame="CARD"), "payment_dialog_not_payment_tab"),  # a card frame
    (_confirm("Pay?", url=STRIPE + "/v3/frame"), "payment_dialog_not_payment_tab"),  # a card origin, no frame named
    (_confirm("Pay?", session="S-other"), "payment_dialog_not_payment_tab"),  # another tab
    (_confirm("Pay?", url=""), "payment_dialog_not_payment_tab"),  # an unknown source
    (_confirm("Pay?", frame="MSUB"), "payment_dialog_not_payment_tab"),  # a child frame of the shop page
    (_confirm("Pay?", frame=None), "payment_dialog_not_payment_tab"),  # no frame id: top identity unproven
    (_confirm("Leave?", kind="beforeunload"), "payment_dialog_not_confirm"),  # confirms only
])
def test_payment_dialog_answers_only_a_confirm_of_the_payment_tab_top_page(dialog, error):
    sup = _paid([CHECKOUT])
    sup.dialogs = [dialog]
    reported = done.payment_done(sup).get("dialog_pending") or {}  # another tab's dialog: a count only
    assert reported.get("on_payment_tab_top", False) is (dialog.type == "beforeunload")
    assert done.payment_dialog(sup, "d-1", True)["error_type"] == error
    assert not sup.sent("respond_to_dialog")


def test_dialog_text_is_redacted_of_the_card_before_masking():
    sup, other = FakeSup([CHECKOUT]), FakeSup([CHECKOUT])
    done.register_dialog_card_values({"card_number": "4000056655665556", "cvc": "737", "exp_month": "3",
                                      "exp_year": "2031", "cardholder_name": "Ada Testperson"}, sup)
    text = done._dialog_text("Card 4000056655665556 / 3/31 / 03 / 2031, CVC 737, ADA TESTPERSON: place order?", sup)
    assert "737" not in text and "3/31" not in text and "03 / 2031" not in text
    assert "testperson" not in text.lower() and "5556" not in text
    # no oracle: every 3+ digit run is masked alike, and another slot's dialogs never see this card's values
    probe = "CVC 735 736 737 738? Ada Testperson?"
    assert done._dialog_text(probe, other) == "CVC ••• ••• ••• •••? Ada Testperson?"
    assert done._dialog_text(probe, sup) == "CVC ••• ••• ••• •••? «redacted-card»?"
    # Codex r5 P3: a date-like prefix never leaves the tail of a longer run visible
    masked = done._dialog_text("Order 03/12345, ref 03/123456", other)
    assert not any(ch.isdigit() for ch in masked), masked


def test_a_registered_vault_secret_with_digits_is_scrubbed_whole_and_pure_digits_leave_no_oracle():
    # r5 P2: the digit mask ran before the exact-value scrub, so a registered secret
    # with a digit run inside it reached the model with only its digits masked
    from agent import redact

    sup = FakeSup([CHECKOUT])
    redact.register_vault_redaction_value("correcthorse947battery")
    redact.register_vault_redaction_value("947")
    try:
        out = done.redact_dialog_text("Your password correcthorse947battery is wrong", sup)
        assert out == "Your password «redacted-vault-secret» is wrong"
        assert "correcthorse" not in out and "battery" not in out and "947" not in out
        # no oracle: a registered pure-digit value is masked exactly like an unregistered one
        assert done.redact_dialog_text("code 947 ok", sup) == done.redact_dialog_text(
            "code 947 ok", FakeSup([CHECKOUT])) == "code ••• ok"
    finally:
        redact.clear_vault_redaction_values()


def test_a_confirm_on_the_payment_tab_is_reported_while_the_page_session_is_elsewhere():
    # r5 P3: a reconnect re-attached the supervisor's page session to another tab, so the
    # payment tab's own held confirm showed only as other_dialogs_pending, and payment_dialog
    # refused it
    sup = _paid([CHECKOUT])
    sup.targets["S-page"] = "OTHER"  # the supervisor's page session sits on another tab
    sup.targets["S-pay"] = "TAB"  # a live session of the payment tab
    sup.dialogs = [
        _confirm(
            "Other tab?", opened=0.5, did="d-0", url="https://x.test/", session="S-x"
        ),
        _confirm("Place order for 22.66 EUR?", session="S-pay"),
        _confirm(
            "Card?",
            opened=1.5,
            did="d-2",
            url=STRIPE + "/v3/frame",
            frame="CARD",
            session="S-pay",
        ),
    ]
    out = done.payment_done(sup)
    assert out["error_type"] == "payment_dialog_pending"
    assert out["other_dialogs_pending"] == 1 and out["dialogs_pending"] == 2
    held = out["dialog_pending"]
    assert (held["id"], held["type"], held["origin"]) == ("d-1", "confirm", SHOP)
    assert (held["on_payment_tab_top"], held["on_shop_page"]) == (True, True)
    # no approved total recorded: the amount stays masked (r5 P3)
    assert held["text"] == "Place order for ••.•• EUR?"
    assert done.payment_dialog(sup, "d-1", True)["answered"] == "accept"
    # the refusals stand here too: a card-frame confirm is never accepted
    assert done.payment_dialog(sup, "d-2", True)["error_type"] == "payment_dialog_not_payment_tab"
    assert not [c for c in sup.sent("respond_to_dialog") if c[1]["dialog_id"] == "d-2"]


def test_only_the_shop_page_confirm_can_be_accepted():
    # peer r4 P3-A: a confirm on a bank (3-D Secure) page at the top of the payment tab may only be dismissed
    sup = _paid([ACS])
    sup.dialogs = [_confirm("Approve 922.66 EUR to Other Merchant?", url="https://acs.bank.test/challenge")]
    held = done.payment_done(sup)["dialog_pending"]
    assert (held["on_payment_tab_top"], held["on_shop_page"]) == (True, False)
    assert done.payment_dialog(sup, "d-1", True)["error_type"] == "payment_dialog_not_shop_page"
    assert not sup.sent("respond_to_dialog")
    assert done.payment_dialog(sup, "d-1", False)["answered"] == "dismiss"


def test_dialogs_on_other_tabs_never_hold_up_the_handover():
    # peer r4 P3-B: an older confirm on another tab is counted only; the payment tab's own dialog is reported
    sup = _paid([CHECKOUT])
    sup.dialogs = [_confirm("Other?", opened=0.5, did="d-0", url="https://x.test/", session="S-other"),
                   _confirm("Place order?", did="d-1")]
    out = done.payment_done(sup)
    assert out["dialog_pending"]["id"] == "d-1" and out["dialogs_pending"] == 1 and out["other_dialogs_pending"] == 1
    sup = _paid([CONFIRM])
    sup.dialogs = [_confirm("Other?", did="d-0", url="https://x.test/", session="S-other")]
    assert done.payment_done(sup)["success"] is True  # handed over, not stalled by the other tab


def test_a_beforeunload_is_allowed_only_while_payment_done_closes_the_tab():
    sup = _paid([CONFIRM])
    assert done.payment_done(sup)["payment_tab_closed"] is True
    # bound to the page session of the tab being closed, only while it closes
    assert sup.closing == ["S-page"] and sup._payment_handover_closing is None


def test_a_closed_recorded_tab_never_makes_payment_done_ambiguous():
    # peer r2 P2-a: tab A was paid and closed outside payment_done; the retry on tab TAB is handed over
    sup = _paid([CONFIRM])
    done.record_payment(sup, SimpleNamespace(page_id="TABA", merchant_origin=SHOP, tree=CHECKOUT,
                                             documents=(SimpleNamespace(frame_id="CARD", origin=STRIPE),),
                                             unreviewed_origins=()))
    sup.extra_tabs = [{"targetId": "TABA", "type": "page", "url": SHOP + "/checkout"}]
    assert done.payment_done(sup)["error_type"] == "payment_done_ambiguous"  # both still open: ambiguous
    sup.extra_tabs = []  # TABA was closed
    assert done.payment_done(sup)["success"] is True


def test_only_the_owner_approved_total_is_shown_in_a_dialog():
    text = "Place order for 122.66 EUR (122,66)? Not 122.67, 922.66, 1.122,66 or 122.665."
    sup = _paid([CHECKOUT])
    unrecorded = done._dialog_text(text, sup)  # no approved amount: every amount stays masked
    assert "122" not in unrecorded and "922" not in unrecorded
    done.record_approved_amount(sup, "TAB", "122.66", "EUR")
    shown = done._dialog_text(text, sup)
    assert shown == "Place order for 122.66 EUR (122,66)? Not •••.••, •••.••, 1.•••,66 or •••.•••."
    assert "122.66" not in done._dialog_text(text, _paid([CHECKOUT]))  # another slot's payment: masked


def test_other_amounts_stay_masked_and_only_the_approved_total_shows():
    # r5 P3: "9.99", "12.34" and "9,99" carried no 3+ digit run and passed verbatim,
    # while the shipped schema text promises every amount but the total stays masked
    sup = _paid([CHECKOUT])
    done.record_approved_amount(sup, "TAB", "22.66", "EUR")
    text = "Pay 22.66? Add premium for 9.99/month, was 12.34 (9,99)"
    assert (
        done._dialog_text(text, sup)
        == "Pay 22.66? Add premium for •.••/month, was ••.•• (•,••)"
    )


def test_the_card_window_lasts_until_the_lock_release_is_confirmed(monkeypatch):
    # r8 (owner decision): the length masking of card digits holds only for the payment window. An unlocked
    # answer without a lock this process saw closes nothing; a locked-then-unlocked one (payment_done's
    # lock_released, a recovery close followed by any status read) does, and a mark clear ends a slot's.
    from agent import redact
    from tools import browser_use_cli
    from tools import browser_vault_payment as bvp
    from tools.browser_payment_privacy import clear_payment_surface, mark_payment_session

    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: {"slot_id": "shopping"})
    sup = FakeSup([CHECKOUT])
    card = {"card_number": "4000056655665556", "cvc": "737", "cardholder_name": "Li"}
    try:
        bvp._register_card_redaction(card, sup)
        assert surface.lock_status(sup) == "unlocked"  # never locked here: the window stays
        assert redact.redact_sensitive_text("printed 737 by Li", force=True) == "printed ••• by «redacted-card»"
        sup.status = {"locked": True, "native": True, "tabs": ["TAB"], "unverified": False}
        assert surface.lock_status(sup) == "locked"
        sup.status = dict(UNLOCKED)
        assert done._released(sup)
        assert redact.redact_sensitive_text("printed 737 by Li", force=True) == "printed 737 by Li"
        bvp._register_card_redaction(card, FakeSup([CHECKOUT]))  # a second payment on the slot, then its clear
        mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
        assert clear_payment_surface("shopping", confirmed_by="test") == 1
        assert redact.redact_sensitive_text("printed 737", force=True) == "printed 737"
    finally:
        redact.clear_vault_redaction_values()
