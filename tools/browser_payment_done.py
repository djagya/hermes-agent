"""Native "payment done" handover (payment lockdown design, section 2).

Once a card fill claims the payment surface, browser-control locks the slot for every agent
connection until each owning payment tab is destroyed. ``browser_vault action=payment_done`` is the
runtime's way out after the Pay click, on the supervisor's own (native) socket:

1. Wait, bounded, for the payment tab's top document to be a new document on the shop's origin
   (the checkout's merchant; ``expected_origin`` only when the checkout ran on a provider's own page) that is not a payment-provider or approved
   card origin, with none of this payment's card documents and no card-field frame in its tree,
   fully loaded and unchanged over two polls. Payment-provider and 3-D Secure pages in between are
   waited through. A single-page shop's checkout document counts too once it never held a card
   field and every claimed card frame is gone.
2. Snapshot that document in an isolated world: title, URL, visible text (bounded) and http(s)
   links (bounded). Form controls, editable regions and frames are skipped: no input value is ever
   read, and nothing is captured as pixels.
3. Open a fresh tab at that URL (``Target.createTarget``), then close the payment tab
   (``Target.closeTarget``), which is what lifts the lock.
4. Report ``lock_released`` only when browser-control's status proves the slot is unlocked.

No step is retried automatically and nothing here touches a card value.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import weakref
from dataclasses import dataclass, replace
from typing import Any, Optional
from urllib.parse import urlsplit

from agent.vault_payment_policy import (
    PAYMENT_PROVIDERS,
    UNSUPPORTED_PROVIDERS,
    PaymentPolicyError,
    https_origin,
    provider_for_origin,
)
# Re-exported for the card fill and tests: the card's dialog values live in browser_payment_card_values.
from tools.browser_payment_card_values import (
    dialog_card_values,
    has_dialog_card_values,
    register_dialog_card_values,
    slot_of,
)
# parse_lock_status is re-exported: the one lock-state reader lives in browser_payment_surface.
from tools.browser_payment_surface import _wait, lock_status, parse_lock_status

logger = logging.getLogger(__name__)

_WAIT_S = 120.0  # the whole wait for a confirmation document
_POLL_S = 1.5  # between probes; a document must look the same on two consecutive probes
_PROBE_TIMEOUT_S = 20.0
_RELEASE_WAIT_S = 10.0  # browser-control's own watcher must see the tab destroyed
_RELEASE_POLL_S = 0.5
MAX_TEXT = 20_000
MAX_LINKS = 200
MAX_LINK_TEXT = 200
MAX_TITLE = 300
MAX_URL = 8192
MAX_LINK_URL = 2048
MAX_SCAN = 50_000  # elements the card-input scan visits (shadow roots included) before it refuses
MAX_SHADOW_DEPTH = 16
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


@dataclass(frozen=True)
class _Payment:
    """What a confirmed claim recorded about one payment tab (no value, only page structure)."""

    tab: str  # the page target holding the checkout
    merchant: str
    loader: str  # the top document's loader when the card was claimed
    documents: frozenset  # (frame id, loader) of each card document
    receiving: frozenset  # non-merchant origins that received card fields (reviewed or owner-approved)
    merchant_fields: bool = False  # the merchant document itself received card fields
    # The cart total and currency the owner approved for this payment ("122.66", "EUR"), "" when none: the one
    # amount a dialog's text may show unmasked.
    amount: str = ""
    currency: str = ""

    @property
    def frame_ids(self) -> frozenset:
        return frozenset(fid for fid, _loader in self.documents)


_LOCK = threading.Lock()
_PAYMENTS: weakref.WeakKeyDictionary[Any, dict] = weakref.WeakKeyDictionary()


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _url_origin(url: Any) -> str:
    """``scheme://host[:port]`` of an http(s) URL, else ''."""
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return ""
    authority = f"[{host}]" if ":" in host else host
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{authority}" + (f":{port}" if port not in (None, default) else "")


def record_payment(supervisor: Any, inspected: Any) -> None:
    """Called once control confirmed a claim: remember the payment tab and its card documents for
    ``payment_done``. Never raises; an inspection it cannot read records nothing (payment_done then
    refuses with ``payment_done_no_payment``)."""
    try:
        tree, merchant = tuple(inspected.tree), inspected.merchant_origin
        loaders = {row[0]: row[1] for row in tree}
        docs = tuple(inspected.documents)
        hosted = [d for d in docs if d.origin != merchant]
        payment = _Payment(
            tab=inspected.page_id,
            merchant=https_origin(merchant),
            loader=tree[0][1],
            documents=frozenset((d.frame_id, loaders.get(d.frame_id, "")) for d in docs),
            receiving=frozenset(d.origin for d in hosted)
            | frozenset(getattr(inspected, "unreviewed_origins", ()) or ()),
            merchant_fields=len(hosted) != len(docs),
        )
        if not (_valid_id(payment.tab) and isinstance(payment.loader, str) and payment.loader):
            raise ValueError("payment tab or loader unknown")
    except Exception as exc:  # health: allow BLE001 -- the claim stands; only payment_done is unavailable
        logger.info("payment_done: claim not recorded (%s)", type(exc).__name__)
        return
    with _LOCK:
        tabs = _PAYMENTS.setdefault(supervisor, {})
        old = tabs.get(payment.tab)
        if old is not None:  # a second fill on the same tab: every card document of both counts
            payment = _Payment(payment.tab, payment.merchant, payment.loader, old.documents | payment.documents,
                               old.receiving | payment.receiving, old.merchant_fields or payment.merchant_fields)
        tabs[payment.tab] = payment


_APPROVED_TOTAL = re.compile(r"(0|[1-9][0-9]{0,6})\.[0-9]{2}")


def record_approved_amount(supervisor: Any, tab: Any, total: Any, currency: Any) -> None:
    """Called by the card fill after a confirmed claim, with the cart total and currency the owner approved.
    Anything malformed records nothing (every amount then stays masked)."""
    if not (isinstance(total, str) and _APPROVED_TOTAL.fullmatch(total) and isinstance(currency, str)
            and re.fullmatch(r"[A-Z]{3}", currency)):
        return
    with _LOCK:
        tabs = _PAYMENTS.get(supervisor)
        payment = tabs.get(tab) if tabs is not None else None
        if payment is not None:
            tabs[tab] = replace(payment, amount=total, currency=currency)


def _approved_amounts(supervisor: Any) -> frozenset:
    """``(integer part, cents)`` of each owner-approved total recorded for ``supervisor``'s payments."""
    if supervisor is None:
        return frozenset()
    with _LOCK:
        try:
            payments = list((_PAYMENTS.get(supervisor) or {}).values())
        except TypeError:
            return frozenset()
    return frozenset(tuple(p.amount.split(".")) for p in payments if p.amount)


def _forget(supervisor: Any, tab: str) -> None:
    with _LOCK:
        tabs = _PAYMENTS.get(supervisor)
        if tabs is not None:
            tabs.pop(tab, None)


def _card_origins(payment: _Payment) -> frozenset:
    """Every origin a top-level confirmation may not be on: reviewed and unsupported providers'
    card origins, plus the origins that received card fields in this payment."""
    registry = {o for p in PAYMENT_PROVIDERS for o in p.origins} | set(UNSUPPORTED_PROVIDERS)
    return frozenset(registry) | payment.receiving


# Provider frames known to hold no card field (exact origin, full path match). Any other frame on a card
# origin counts as a card frame: an unknown provider frame fails closed.
_HARMLESS_FRAMES = {
    # Stripe.js's hidden controller and metrics frames, present on many shop pages that load Stripe.js
    "https://js.stripe.com": re.compile(r"/v3/(controller|controller-with-preconnect|m-outer)-[A-Za-z0-9]{1,64}\.html"),
}


def _harmless(origin: str, url: Any) -> bool:
    pattern = _HARMLESS_FRAMES.get(origin)
    if pattern is None or _url_origin(url) != origin:
        return False
    return bool(pattern.fullmatch(urlsplit(url).path))


def _card_frame(row: tuple, payment: _Payment) -> bool:
    """A frame that is (or can be) a card document: one of this payment's own card documents, or any frame
    on a card origin (every reviewed or unsupported provider, and the origins that received card fields in
    this payment) unless it is a known harmless provider frame (``_HARMLESS_FRAMES``)."""
    fid, loader, url, origin = row[0], row[1], row[2], row[3]
    if (fid, loader) in payment.documents:
        return True
    card = _card_origins(payment)
    return any(o in card and not _harmless(o, url) for o in {origin, _url_origin(url)} - {""})


def _safe_top(tree: tuple, payment: _Payment, expected: str) -> bool:
    """The payment tab shows a document on the shop's origin with no card document in its tree: a new
    document, or (a single-page shop) the checkout's own document when it never received a card field and
    every card frame claimed for this payment is gone from the tree."""
    if not tree or len(tree[0]) != 5:
        return False
    _fid, loader, url, origin, parent = tree[0]
    if parent or not isinstance(loader, str) or not loader:
        return False
    if loader == payment.loader and (payment.merchant_fields
                                     or any(row[0] in payment.frame_ids for row in tree)):
        return False
    if origin != expected or _url_origin(url) != expected or expected in _card_origins(payment):
        return False
    return not any(_card_frame(row, payment) for row in tree)


# Runs in a fresh isolated world of the confirmation document. Reads text nodes, document.title,
# location and anchor hrefs only: form controls (and their values), editable regions, frames,
# scripts and hidden subtrees are skipped. Card-like inputs are only counted, never read.
SNAPSHOT_JS = r"""(() => {
  const MAX_TEXT = %MAX_TEXT%, MAX_LINKS = %MAX_LINKS%, MAX_LINK_TEXT = %MAX_LINK_TEXT%;
  const MAX_TITLE = %MAX_TITLE%, MAX_URL = %MAX_LINK_URL%;
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'INPUT', 'TEXTAREA', 'SELECT', 'OPTION',
    'OPTGROUP', 'DATALIST', 'IFRAME', 'FRAME', 'FRAMESET', 'OBJECT', 'EMBED', 'CANVAS']);
  const BLOCK = new Set(['P', 'DIV', 'LI', 'TR', 'TD', 'TH', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'SECTION',
    'ARTICLE', 'HEADER', 'FOOTER', 'TABLE', 'UL', 'OL', 'DL', 'DT', 'DD', 'BLOCKQUOTE', 'PRE', 'FORM',
    'FIELDSET', 'MAIN', 'NAV', 'ASIDE', 'ADDRESS', 'FIGURE', 'FIGCAPTION', 'HR', 'LABEL']);
  const shown = (el) => { const s = getComputedStyle(el); return !s || (s.display !== 'none' && s.visibility !== 'hidden'); };
  const collect = (root, limit, nodes) => {
    const out = []; let size = 0, budget = nodes, cut = false;
    const walk = (node, depth) => {
      if (cut) return;
      if (size >= limit || --budget < 0 || depth > 200) { cut = true; return; }
      if (node.nodeType === 3) {
        const t = String(node.data).replace(/\s+/g, ' ');
        if (t.trim()) { out.push(t); size += t.length; }
        return;
      }
      if (node.nodeType !== 1 && node.nodeType !== 11) return;
      let tag = '';
      if (node.nodeType === 1) {
        tag = String(node.tagName).toUpperCase();
        if (SKIP.has(tag) || node.isContentEditable === true || !shown(node)) return;
        if (tag === 'BR') { out.push('\n'); return; }
      }
      let kids = node.childNodes || [];
      if (node.nodeType === 1 && node.shadowRoot) kids = [node.shadowRoot];
      else if (tag === 'SLOT' && typeof node.assignedNodes === 'function') {
        const assigned = node.assignedNodes({flatten: true});
        if (assigned.length) kids = assigned;
      }
      for (let i = 0; i < kids.length && !cut; i++) walk(kids[i], depth + 1);
      if (BLOCK.has(tag)) out.push('\n');
    };
    walk(root, 0);
    const text = out.join('').replace(/[ \t]*\n\s*/g, '\n').replace(/[ \t]+/g, ' ').trim();
    return {text: text.slice(0, limit), cut: cut || text.length > limit};
  };
  const CARD_AC = /(^|\s)cc-(number|csc|exp|exp-month|exp-year)(\s|$)/i;
  const CARD_NAME = /card[-_ ]?number|cc[-_ ]?num|(^|[^a-z])(cvc|cvv|csc)([^a-z]|$)/i;
  // Card-like inputs anywhere in the document, open shadow roots included; past the bound -1 (refused).
  let cardFields = 0, scanned = 0;
  const scan = (root, depth) => {
    if (depth > %MAX_SHADOW_DEPTH%) { cardFields = -1; return; }
    for (const el of root.querySelectorAll('*')) {
      if (cardFields < 0) return;
      if (++scanned > %MAX_SCAN%) { cardFields = -1; return; }
      const tag = String(el.tagName).toUpperCase();
      if (tag === 'INPUT' || tag === 'SELECT') {
        const ac = String(el.getAttribute('autocomplete') || '');
        const named = String(el.getAttribute('name') || '') + ' ' + String(el.getAttribute('id') || '');
        if (CARD_AC.test(ac) || CARD_NAME.test(named)) cardFields++;
      }
      if (el.shadowRoot) scan(el.shadowRoot, depth + 1);
    }
  };
  scan(document, 0);
  const links = []; const seen = new Set(); let linksCut = false;
  for (const a of document.querySelectorAll('a[href], area[href]')) {
    if (links.length >= MAX_LINKS) { linksCut = true; break; }
    const href = a.href;
    if (typeof href !== 'string' || href.length > MAX_URL) continue;
    let u; try { u = new URL(href); } catch (e) { continue; }
    if ((u.protocol !== 'http:' && u.protocol !== 'https:') || u.href.length > MAX_URL) continue;
    let text = collect(a, MAX_LINK_TEXT, 2000).text.replace(/\s+/g, ' ');
    if (!text) text = String(a.getAttribute('aria-label') || a.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
    text = text.slice(0, MAX_LINK_TEXT);
    const key = u.href + '\u0000' + text;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push({url: u.href, text});
  }
  const root = document.body || document.documentElement;
  const page = root ? collect(root, MAX_TEXT, 100000) : {text: '', cut: false};
  return {title: String(document.title || '').replace(/\s+/g, ' ').trim().slice(0, MAX_TITLE),
          url: String(location.href), origin: String(location.origin), ready: String(document.readyState),
          text: page.text, text_truncated: page.cut, links, links_truncated: linksCut, card_fields: cardFields};
})()"""
for _name, _value in (("MAX_TEXT", MAX_TEXT), ("MAX_LINKS", MAX_LINKS), ("MAX_LINK_TEXT", MAX_LINK_TEXT),
                      ("MAX_TITLE", MAX_TITLE), ("MAX_LINK_URL", MAX_LINK_URL), ("MAX_SCAN", MAX_SCAN),
                      ("MAX_SHADOW_DEPTH", MAX_SHADOW_DEPTH)):
    SNAPSHOT_JS = SNAPSHOT_JS.replace(f"%{_name}%", str(_value))

_SNAPSHOT_KEYS = frozenset({"title", "url", "origin", "ready", "text", "text_truncated", "links",
                            "links_truncated", "card_fields"})


def _valid_snapshot(value: Any, expected: str) -> Optional[dict]:
    """The snapshot as returned to the model, or None when it is not exactly the script's shape, not
    fully loaded, not on ``expected``, or shows card inputs (the caller keeps waiting)."""
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_KEYS:
        return None
    title, url, text, links = value["title"], value["url"], value["text"], value["links"]
    if not (isinstance(title, str) and len(title) <= MAX_TITLE and isinstance(url, str) and len(url) <= MAX_URL
            and isinstance(text, str) and len(text) <= MAX_TEXT and isinstance(links, list)
            and len(links) <= MAX_LINKS and type(value["text_truncated"]) is bool
            and type(value["links_truncated"]) is bool):
        return None
    if value["ready"] != "complete" or type(value["card_fields"]) is not int or value["card_fields"] != 0:
        return None
    if value["origin"] != expected or _url_origin(url) != expected:
        return None
    clean = []
    for link in links:
        if not (isinstance(link, dict) and set(link) == {"url", "text"} and isinstance(link["url"], str)
                and len(link["url"]) <= MAX_LINK_URL and _url_origin(link["url"])
                and isinstance(link["text"], str) and len(link["text"]) <= MAX_LINK_TEXT):
            return None
        clean.append({"url": link["url"], "text": link["text"]})
    return {"url": url, "title": title, "text": text, "text_truncated": value["text_truncated"],
            "links": clean, "links_truncated": value["links_truncated"]}


def has_payment(supervisor: Any) -> bool:
    with _LOCK:
        return bool(_PAYMENTS.get(supervisor))


def card_frame_ids(supervisor: Any) -> frozenset:
    """Frame ids of the card documents of every payment this supervisor claimed and has not handed over."""
    with _LOCK:
        tabs = list((_PAYMENTS.get(supervisor) or {}).values())
    return frozenset(fid for payment in tabs for fid in payment.frame_ids)


async def top_document(sup: Any, page_sid: str, tree: tuple) -> Optional[tuple]:
    """For payment_controls / payment_click while the slot is locked: ``(frame id, session, ref key)`` of
    the supervised page's top document when that page is a recorded payment tab and its top document is
    not a card frame (not on a provider or card-receiving origin, not one of this payment's card
    documents, not a provider card-field path). A 3-D Secure page on the bank's own origin qualifies.
    The ref key binds refs to this document (its loader). None otherwise, and on any failure."""
    try:
        with _LOCK:
            tabs = dict(_PAYMENTS.get(sup) or {})
        if not tabs or not tree or len(tree[0]) != 5:
            return None
        info = (await sup._cdp("Target.getTargetInfo", session_id=page_sid))["result"]["targetInfo"]
        payment = tabs.get(info.get("targetId"))
        fid, loader, url, origin, parent = tree[0]
        if payment is None or parent or not (isinstance(loader, str) and loader and _valid_id(fid)):
            return None
        if not origin or origin != _url_origin(url) or origin in _card_origins(payment):
            return None
        if _card_frame(tree[0], payment):
            return None
        return fid, page_sid, f"{fid}|{loader}"
    except Exception as exc:  # health: allow BLE001 -- fail closed: the top document is not offered
        logger.info("payment controls: top document not offered (%s)", type(exc).__name__)
        return None


def _refusal(error_type: str, message: str, **extra) -> dict:
    return {"success": False, "error_type": error_type, "error": message, **extra}


_NO_PAYMENT = ("No card payment by Hermes is recorded for this browser connection, so there is no payment tab to "
               "hand over. Nothing was done. If a checkout is still locked, close its tab or ask the user.")
_AMBIGUOUS = ("Card payments ran in more than one tab of this browser, so Hermes cannot tell which one to hand "
              "over. Nothing was done. Close the payment tabs you no longer need, or ask the user.")
_ORIGIN_INVALID = ("expected_origin must be the shop's exact HTTPS origin (scheme://host[:port]) and not a payment "
                   "provider or card form origin. Nothing was done.")
_ORIGIN_REQUIRED = ("This checkout ran on a payment provider's own page, so the shop's origin is not known. Nothing "
                    "was done. Call payment_done again with expected_origin set to the shop's HTTPS origin where "
                    "the order confirmation will show.")
_NOT_REACHED = ("No order confirmation was reached on the payment tab within the wait: it still shows a checkout, "
                "payment provider or bank (3-D Secure) page, a page on another site, or a page that is still "
                "loading. The browser stays locked; nothing was clicked, closed or retried. Do not press Pay again. "
                "url shows the site the tab is on (origin only while locked). If a bank check is waiting, ask the "
                "user to finish it, then call payment_done again; otherwise ask the user (closing the payment tab "
                "with browser_cdp Target.closeTarget also ends the lock, without a snapshot).")
_TAB_GONE = ("The payment tab is no longer open, so there is no confirmation page to read. Nothing was done. "
             "lock_released says whether browser control has unlocked the browser; check the order with the "
             "shop or the user's email.")
_IGNORED = ("expected_origin was ignored: it applies only when the checkout ran on a payment provider's own page. "
            "The confirmation was looked for on the checkout's own origin.")
_NO_NEW_TAB = ("The confirmation was read, but a new tab could not be opened, so the payment tab was left open and "
               "the browser stays locked. Use text and links; close the payment tab or ask the user to end the lock.")
_CLOSE_FAILED = ("The confirmation was read and a new tab opened, but closing the payment tab failed, and browser "
                 "control has not confirmed the unlock. Close the payment tab or ask the user.")
_UNPROVEN = ("The confirmation was read, the new tab opened and the payment tab closed, but browser control has not "
             "yet confirmed the unlock. Use text and links; if browser actions are still refused, ask the user.")


def _drop_closed_tabs(supervisor: Any) -> list:
    """Forget recorded payment tabs that are no longer open (closed by the user, by the agent's recovery
    call, or by a close whose answer was lost), read fresh from the browser. A failed read keeps them all."""
    with _LOCK:
        recorded = list((_PAYMENTS.get(supervisor) or {}).keys())
    if not recorded:
        return []
    try:
        from tools.browser_payment_surface import _page_tabs

        open_tabs = {t["targetId"] for t in _wait(_page_tabs(supervisor), supervisor._loop, timeout=10)}
    except Exception as exc:  # health: allow BLE001 -- unknown: keep every record (an ambiguous refusal at worst)
        logger.info("payment_done: open tabs not read (%s)", type(exc).__name__)
        return []
    gone = [tab for tab in recorded if tab not in open_tabs]
    for tab in gone:
        _forget(supervisor, tab)
    return gone


def _payment_for(supervisor: Any) -> tuple[Optional[_Payment], Optional[dict]]:
    gone = _drop_closed_tabs(supervisor)
    with _LOCK:
        tabs = dict(_PAYMENTS.get(supervisor) or {})
    if not tabs and gone:
        return None, _refusal("payment_tab_gone", _TAB_GONE, lock_released=_released(supervisor))
    if not tabs:
        return None, _refusal("payment_done_no_payment", _NO_PAYMENT)
    if len(tabs) > 1:
        return None, _refusal("payment_done_ambiguous", _AMBIGUOUS)
    return next(iter(tabs.values())), None


def _expected(payment: _Payment, expected_origin: str) -> tuple[str, Optional[dict], bool]:
    """``(origin the confirmation must be on, refusal, expected_origin ignored)``. ``expected_origin`` is
    honoured only when the checkout itself ran on a payment provider's page (its merchant origin is a card
    origin), the one case where the shop's origin is unknown; otherwise the merchant's origin stands."""
    if payment.merchant not in _card_origins(payment):
        return payment.merchant, None, bool(expected_origin)
    if not expected_origin:
        return "", _refusal("payment_done_origin_invalid", _ORIGIN_REQUIRED), False
    try:
        expected = https_origin(expected_origin)
    except PaymentPolicyError:
        return "", _refusal("payment_done_origin_invalid", _ORIGIN_INVALID), False
    if expected in _card_origins(payment):
        return "", _refusal("payment_done_origin_invalid", _ORIGIN_INVALID), False
    return expected, None, False


def _bounded(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


async def _tab_session(sup: Any, tab: str) -> tuple[str, bool]:
    """A session on the payment tab: the supervisor's page session when it is that tab, else a
    temporary one (``True``: the caller detaches it)."""
    with sup._state_lock:
        sid = sup._page_session_id if sup._active else None
    if sid:
        info = (await sup._cdp("Target.getTargetInfo", session_id=sid))["result"]["targetInfo"]
        if info.get("targetId") == tab:
            return sid, False
    sid = (await sup._cdp("Target.attachToTarget", {"targetId": tab, "flatten": True}))["result"]["sessionId"]
    try:
        await sup._enable_page_domains(sid, timeout=5)
    except BaseException:
        await sup._release_payment_sessions({"tab": sid})
        raise
    return sid, True


async def _snapshot(sup: Any, sid: str, frame_id: str) -> Any:
    ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "hermes-payment-done"},
                          session_id=sid))["result"]["executionContextId"]
    reply = (await sup._cdp("Runtime.evaluate", {"expression": SNAPSHOT_JS, "contextId": ctx,
                                                  "returnByValue": True}, session_id=sid, timeout=10))["result"]
    if reply.get("exceptionDetails"):
        return None
    return (reply.get("result") or {}).get("value")


async def _probe(sup: Any, payment: _Payment, expected: str, previous: Optional[tuple]) -> tuple:
    """One look at the payment tab: ``(state, info, key)`` with state ``gone`` | ``wait`` |
    ``settling`` (safe, first sighting of document ``key``) | ``ready`` (safe twice in a row, snapshot
    taken and the tree unchanged after it)."""
    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    tab = next((t for t in infos if t.get("targetId") == payment.tab and t.get("type") == "page"), None)
    if tab is None:
        return "gone", {}, None
    seen = {"url": _bounded(tab.get("url"), MAX_URL), "title": _bounded(tab.get("title"), MAX_TITLE)}
    sid, temporary = await _tab_session(sup, payment.tab)
    child_sessions: dict = {}
    try:
        try:
            tree = await sup._payment_tree(sid, child_sessions)
        except PaymentPolicyError:  # too many frames or nesting: never judged safe
            return "wait", seen, None
        if not _safe_top(tree, payment, expected):
            return "wait", seen, None
        key = (tree[0][0], tree[0][1])
        if key != previous:
            return "settling", seen, key
        page = _valid_snapshot(await _snapshot(sup, sid, tree[0][0]), expected)
        if page is None or await sup._payment_tree(sid, child_sessions) != tree:
            return "wait", seen, None
        page["browser_context"] = tab.get("browserContextId")
        return "ready", page, key
    finally:
        await sup._release_payment_sessions(child_sessions)
        if temporary:
            await sup._release_payment_sessions({"tab": sid})


async def _open_tab(sup: Any, url: str, context: Any) -> str:
    params = {"url": url, **({"browserContextId": context} if _valid_id(context) else {})}
    created = (await sup._cdp("Target.createTarget", params))["result"]["targetId"]
    if not _valid_id(created):
        raise RuntimeError("createTarget answered without a target id")
    return created


async def _close_tab(sup: Any, tab: str) -> bool:
    """Close the payment tab; returns whether the supervisor's page session was on it. dlz: a beforeunload is
    accepted only for this close and only from this tab's session (``_payment_handover_closing``)."""
    with sup._state_lock:
        sid = sup._page_session_id if sup._active else None
    on_tab = False
    if sid:
        try:
            info = (await sup._cdp("Target.getTargetInfo", session_id=sid))["result"]["targetInfo"]
            on_tab = info.get("targetId") == tab
        except Exception:  # health: allow BLE001 -- only decides whether to follow to the new tab
            on_tab = False
    sup._payment_handover_closing = sid if on_tab else None
    try:
        await sup._cdp("Target.closeTarget", {"targetId": tab})
    finally:
        sup._payment_handover_closing = None
    return on_tab


async def _follow(sup: Any, new_tab: str) -> None:
    """Best effort: point the supervisor at the new tab once its page (the payment tab) is closed."""
    with sup._state_lock:
        old = sup._page_session_id
    sid = await sup._attach_page(new_tab, timeout=5)
    with sup._state_lock:
        if sup._page_session_id == old:
            sup._page_session_id = sid


def _redacted_page(page: dict) -> dict:
    """The confirmation snapshot as a browser tool result (agent.redact, ``vault="browser"``): the card's
    lengths and holder, and every registered value, scrubbed from its text, title, URL and links."""
    from agent.redact import redact_sensitive_text

    def scrub(value: Any) -> Any:
        return redact_sensitive_text(value, force=True, vault="browser") if isinstance(value, str) else value

    out = {k: scrub(v) for k, v in page.items() if k != "links"}
    if "links" in page:
        out["links"] = [{k: scrub(v) for k, v in link.items()} for link in page["links"]]
    return out


def _released(supervisor: Any) -> bool:
    """Poll control's status (bounded) until it proves the slot unlocked."""
    deadline = time.monotonic() + _RELEASE_WAIT_S
    while True:
        if lock_status(supervisor) == "unlocked":
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(_RELEASE_POLL_S, left))


_DIALOG_TYPES_HELD = ("confirm", "beforeunload")
MAX_DIALOG_TEXT = 200
# dlz: the saved card's values a dialog text may quote (number, CVC, holder name, expiry forms) are kept per
# supervisor and per slot by tools.browser_payment_card_values, in memory and for dialog text only (an exact
# scrub of every tool result would blank common names and dates). Every digit run of 3+ and every date-like
# group is masked first, so no scrub difference tells a value apart.

_DIGITS_MASKED = re.compile(r"\d{1,2}\s*[/\-]\s*\d{2,}|\d{3,}")


def payment_text_exposed(supervisor: Any) -> bool:
    """Whether page-originated text seen through ``supervisor`` needs the payment masking (every 3+ digit
    run, date-like group and amount, plus its registered card values). True while its slot is locked for a
    card payment (the local lock state only — no status read), once a card fill registered values for it, or
    while the persistent capture-guard mark still covers its slot (a mark without a slot id, and every mark
    on a browser without control, is profile-wide; a supervisor without a slot route is exposed while any
    mark of this profile exists). An unreadable answer masks (fail closed)."""
    try:
        from tools.browser_payment_surface import locally_locked

        if supervisor is not None and locally_locked(supervisor):  # no supervisor: no lock of its own, marks decide
            return True
    except Exception:  # health: allow BLE001 -- unknown lock state: mask
        return True
    if has_dialog_card_values(supervisor):
        return True
    try:
        from tools.browser_payment_privacy import slot_has_mark

        return slot_has_mark(slot_of(supervisor))  # no slot route: any mark of this profile
    except Exception:  # health: allow BLE001 -- marks unreadable: mask
        return True


def _mask_digits(text: str) -> str:
    return _DIGITS_MASKED.sub(lambda m: re.sub(r"\d", "•", m.group(0)), text)


# A whole amount token: digits, one decimal separator ("." or ","), two digits, with no digit, "." or "," on
# either side (so "1.122,66" or "122.665" never count as "122.66").
_AMOUNT_TOKEN = re.compile(r"(?<![\d.,])(\d{1,7})([.,])(\d{2})(?![\d.,]?\d)")
_KEPT = "\ue000{}\ue001"  # private-use marks: no redactor or scrub touches them


def _mask_amounts(text: str, supervisor: Any) -> tuple:
    """One pass over every amount token: one that exactly equals an owner-approved total of ``supervisor``
    (either separator) is set aside behind a keep-mark, every other one is masked digit-for-digit (the
    separator stays, like the date mask), so no amount but an approved total is readable in a dialog."""
    approved, kept = _approved_amounts(supervisor), []

    def repl(m):
        if approved and (m.group(1), m.group(3)) in approved:
            kept.append(m.group(0))
            return _KEPT.format(len(kept) - 1)
        return re.sub(r"\d", "\u2022", m.group(0))

    return _AMOUNT_TOKEN.sub(repl, text), kept


def redact_dialog_text(text: Any, supervisor: Any = None) -> str:
    """Any dialog text the model can see. Registered vault values with a non-digit character are scrubbed
    whole from the RAW text first (pure-digit values, like a card number or CVC, are not: a placeholder
    would mark which digit run was the secret). Then every amount token but an approved total is masked and
    set aside, every run of 3+ digits and every date-like group (``03/31``, ``3 - 2031``) is masked, so no
    exact-value scrub after it can mark which number was the CVC, the card number or the expiry; then the
    forced secret redactor, then the card values registered for ``supervisor`` (its own and its slot's;
    case-insensitive; one shorter than 3 characters as a whole word), and the mask once more over what those left. The one exception: a token exactly
    equal to the cart total the owner approved for this supervisor's payment stays readable."""
    from agent.redact import redact_registered_vault_values, redact_sensitive_text

    raw = redact_registered_vault_values(
        str(text or "").replace("\ue000", "").replace("\ue001", ""), nondigit_only=True, audience="browser"
    )
    text, kept = _mask_amounts(raw, supervisor)
    text = redact_sensitive_text(_mask_digits(text), force=True, vault="browser")
    for value in sorted(dialog_card_values(supervisor), key=len, reverse=True):
        # a value shorter than 3 (a holder "Li") only as a whole word: inside words it would blank every caption
        pattern = re.escape(value) if len(value) >= 3 else rf"(?<!\w){re.escape(value)}(?!\w)"
        text = re.sub(pattern, "«redacted-card»", text, flags=re.IGNORECASE)
    text = _mask_digits(text)
    for i, token in enumerate(kept):
        text = text.replace(_KEPT.format(i), token)
    return text


def _dialog_text(message: Any, supervisor: Any) -> str:
    """A held dialog's text for the model: redacted and masked (``redact_dialog_text``), whitespace collapsed,
    200 characters at most."""
    return re.sub(r"\s+", " ", redact_dialog_text(message, supervisor)).strip()[:MAX_DIALOG_TEXT]


def _held_dialogs(supervisor: Any) -> list:
    """Dialogs the lock holds for an answer (a confirm, or a beforeunload outside a handover), oldest first."""
    try:
        dialogs = list(supervisor.snapshot().pending_dialogs)
    except Exception:  # health: allow BLE001 -- no supervisor state: nothing to report
        return []
    return sorted((d for d in dialogs if getattr(d, "type", "") in _DIALOG_TYPES_HELD), key=lambda d: d.opened_at)


async def _payment_tab_top(sup: Any) -> Optional[dict]:
    """The payment tab's own top document: ``{sid, tab, frame, origin, sessions}`` (its top frame's id and
    origin, and the sessions its dialogs may sit on). The supervisor's page session when it is a recorded
    payment tab, plus that tab's frame sessions. When the page session is not the payment tab (a reconnect
    re-attached it elsewhere), the tab's own session is resolved by the rule ``_tab_session`` applies — a
    live session on that tab — taken from the held dialogs' sessions (``Target.getTargetInfo`` names each
    one's target; a session that died with an old socket names none), never a fresh attach: its id could
    not be the held dialog's, so the confirm would stay unanswerable. Only those sessions count there:
    the supervisor's frame sessions belong to its main page, another tab. None when no recorded payment
    tab answers, or payments are ambiguous."""
    with sup._state_lock:
        page_sid = sup._page_session_id if sup._active else None
    with _LOCK:
        payments = _PAYMENTS.get(sup) or {}
    tab = sid = None
    sessions: set = set()
    children: set = set()
    if page_sid:
        info = (await sup._cdp("Target.getTargetInfo", session_id=page_sid))["result"]["targetInfo"]
        if info.get("targetId") in payments:
            tab, sid = info.get("targetId"), page_sid
    if sid is None:
        if len(payments) != 1:
            return None
        tab = next(iter(payments))
        for dialog in _held_dialogs(sup):
            d_sid = getattr(dialog, "cdp_session_id", None)
            if not d_sid or d_sid in sessions:
                continue
            try:
                info = (await sup._cdp("Target.getTargetInfo", session_id=d_sid))["result"]["targetInfo"]
            except Exception:  # health: allow BLE001 -- a dead session names no target
                continue
            if info.get("targetId") == tab:
                sessions.add(d_sid)
                if sid is None:  # the oldest held dialog on the tab: the one dialog_pending reports
                    sid = d_sid
        if sid is None:
            return None
    else:
        with sup._state_lock:
            children = {f.cdp_session_id for f in getattr(sup, "_frames", {}).values() if f.cdp_session_id}
    frame = (await sup._cdp("Page.getFrameTree", session_id=sid))["result"]["frameTree"]["frame"]
    return {"sid": sid, "tab": tab, "frame": str(frame.get("id") or ""), "origin": _url_origin(frame.get("url")),
            "sessions": {sid} | sessions | children}


def _dialog_place(sup: Any, dialog: Any, top: Optional[dict]) -> tuple:
    """``(on_payment_tab_top, on_shop_page)``. On the top: raised by the recorded payment tab's top document,
    proven by a session of that tab's page (the page session, or the tab's own resolved session) and its top
    frame's id (a dialog without a frame id is not), on that document's origin, which is not a card origin.
    On the shop page: that, and the origin is the payment's merchant."""
    if top is None:
        return False, False
    with _LOCK:
        payment = (_PAYMENTS.get(sup) or {}).get(top["tab"])
    origin = _url_origin(getattr(dialog, "url", ""))
    frame = getattr(dialog, "frame_id", None)
    on_top = (payment is not None and getattr(dialog, "cdp_session_id", None) == top["sid"]
              and bool(top["frame"]) and frame == top["frame"] and bool(origin) and origin == top["origin"]
              and origin not in _card_origins(payment))
    return on_top, on_top and origin == payment.merchant


def _top_or_none(supervisor: Any) -> Optional[dict]:
    try:
        return _wait(_payment_tab_top(supervisor), supervisor._loop, timeout=7)
    except Exception:  # health: allow BLE001 -- unknown source: reported, never answerable
        return None


def _dialog_report(supervisor: Any) -> dict:
    """While held dialogs wait: ``dialog_pending {id, type, origin, on_payment_tab_top, on_shop_page, text}`` for
    the oldest one on the payment tab's sessions with ``dialogs_pending`` (how many there), and
    ``other_dialogs_pending`` (a count only) for those on other tabs or unknown sessions; else {}."""
    held = _held_dialogs(supervisor)
    if not held:
        return {}
    top = _top_or_none(supervisor)
    mine = [d for d in held if top is not None and getattr(d, "cdp_session_id", None) in top["sessions"]]
    out: dict = {}
    if mine:
        dialog = mine[0]
        on_top, on_shop = _dialog_place(supervisor, dialog, top)
        out = {"dialog_pending": {"id": dialog.id, "type": dialog.type,
                                  "origin": _url_origin(getattr(dialog, "url", "")), "on_payment_tab_top": on_top,
                                  "on_shop_page": on_shop, "text": _dialog_text(dialog.message, supervisor)},
               "dialogs_pending": len(mine)}
    if len(held) > len(mine):
        out["other_dialogs_pending"] = len(held) - len(mine)
    return out


_DIALOG_PENDING = ("A page dialog waits for an answer on the payment tab, so the page is blocked (dialog_pending). "
                   "Nothing was clicked or closed and the browser stays locked. Accept it once with payment_dialog "
                   "only if it is a confirm on the shop page (on_shop_page true) that matches the Pay you were told "
                   "to make (e.g. 'Place order for <amount>?'); for anything else (upsells, subscriptions, unknown) "
                   "ask the user. Then call payment_done again.")
_DIALOG_NONE = ("That dialog is not waiting for an answer (answered, dismissed or closed). Nothing was done. Call "
                "payment_controls or payment_done to see the page now.")
_DIALOG_NOT_CONFIRM = "payment_dialog answers only a confirm. Nothing was done; ask the user."
_DIALOG_NOT_PAYMENT_TAB = ("That confirm does not come from the payment tab's own page (a card or bank frame, another "
                           "tab, or an unknown source), so Hermes does not answer it. Nothing was done; ask the user.")
_DIALOG_NOT_SHOP = ("That confirm is not on the shop's own page (e.g. a bank or payment-provider page), so Hermes "
                    "does not accept it; it can only be dismissed (accept false). Nothing was done; ask the user.")
_DIALOG_FAILED = ("The dialog could not be answered (it may have closed already); it is not retried. Call "
                  "payment_controls or payment_done to see where the payment tab is.")


def payment_dialog(supervisor: Any, dialog_id: Any, accept: Any) -> dict:
    """``browser_vault action=payment_dialog dialog_id=… accept=true|false``: one native answer to the confirm
    ``dialog_pending`` showed, only while that exact dialog is still pending, is a confirm, and belongs to the
    recorded payment tab's top document; accepting also needs the shop's own page. One answer per dialog
    (consumed before it is sent), never retried."""
    if type(accept) is not bool or not isinstance(dialog_id, str) or not dialog_id:
        return _refusal("payment_dialog_invalid", "dialog_id (from dialog_pending) and accept (true or false) are "
                                                  "required. Nothing was done.")
    if not has_payment(supervisor):
        return _refusal("payment_dialog_none", _DIALOG_NONE)
    dialog = next((d for d in _held_dialogs(supervisor) if d.id == dialog_id), None)
    if dialog is None:
        return _refusal("payment_dialog_none", _DIALOG_NONE)
    if dialog.type != "confirm":
        return _refusal("payment_dialog_not_confirm", _DIALOG_NOT_CONFIRM)
    on_top, on_shop = _dialog_place(supervisor, dialog, _top_or_none(supervisor))
    if not on_top:
        return _refusal("payment_dialog_not_payment_tab", _DIALOG_NOT_PAYMENT_TAB)
    if accept and not on_shop:
        return _refusal("payment_dialog_not_shop_page", _DIALOG_NOT_SHOP)
    try:
        out = supervisor.respond_to_dialog("accept" if accept else "dismiss", dialog_id=dialog.id)
    except Exception as exc:  # health: allow BLE001 -- CDP text can quote the page; fixed type only
        logger.info("payment_dialog: answer failed (%s)", type(exc).__name__)
        out = {"ok": False}
    if not out.get("ok"):
        return _refusal("payment_dialog_failed", _DIALOG_FAILED)
    return {"success": True, "answered": "accept" if accept else "dismiss", "type": dialog.type, "id": dialog.id}


def payment_done(supervisor: Any, expected_origin: str = "") -> dict:
    """``browser_vault action=payment_done``: see the module docstring."""
    loop = getattr(supervisor, "_loop", None)
    if loop is None or not loop.is_running():
        return _refusal("supervisor_required", "The supervised browser is not attached; nothing was done.")
    payment, refused = _payment_for(supervisor)
    if refused:
        return refused
    expected, refused, ignored = _expected(payment, expected_origin)
    if refused:
        return refused
    # Said in every result below: the checkout's own origin decided, not expected_origin.
    told = {"expected_origin_ignored": True, "expected_origin_note": _IGNORED} if ignored else {}
    deadline = time.monotonic() + _WAIT_S
    previous: Optional[tuple] = None
    seen: dict = {}
    page: Optional[dict] = None
    while page is None:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            state, info, key = _wait(_probe(supervisor, payment, expected, previous), loop,
                                     timeout=max(1.0, min(_PROBE_TIMEOUT_S, left)))
        except Exception as exc:  # health: allow BLE001 -- CDP text can quote the page; keep waiting
            logger.info("payment_done: probe failed (%s)", type(exc).__name__)
            state, info, key = "wait", {}, None
        if state == "gone":
            _forget(supervisor, payment.tab)
            return _refusal("payment_tab_gone", _TAB_GONE, lock_released=_released(supervisor))
        seen = {k: info[k] for k in ("url", "title") if k in info} or seen
        report = _dialog_report(supervisor)
        if "dialog_pending" in report:  # a held dialog blocks the payment tab: hand it over now, not after the wait
            return _refusal("payment_dialog_pending", _DIALOG_PENDING, url=_url_origin(seen.get("url", "")),
                            title="", lock_released=False, **told, **report)
        if state == "ready":
            page = info
            break
        previous = key if state == "settling" else None
        time.sleep(max(0.0, min(_POLL_S, deadline - time.monotonic())))
    if page is None:
        # The tab is still locked: like browser-control for every agent connection, report its site only
        # (a locked page's title and path could carry what a planted script copied there).
        return _refusal("payment_confirmation_not_reached", _NOT_REACHED, url=_url_origin(seen.get("url", "")),
                        title="", lock_released=False, **told, **_dialog_report(supervisor))
    context = page.pop("browser_context", None)
    raw_url = page["url"]  # the tab must open exactly this; only what is returned to the model is scrubbed
    page = _redacted_page(page)  # dlz: while the payment window is still open, before the release below
    result = {"success": True, **page, "new_tab": None, "payment_tab_closed": False, "lock_released": False, **told}
    try:  # the new tab first: the payment tab is closed only once the confirmation has somewhere to go
        result["new_tab"] = _wait(_open_tab(supervisor, raw_url, context), loop, timeout=15)
    except Exception as exc:  # health: allow BLE001 -- fixed text; the payment tab stays open and locked
        logger.info("payment_done: new tab failed (%s)", type(exc).__name__)
        return {**result, "note": _NO_NEW_TAB}
    on_tab = False
    try:
        on_tab = _wait(_close_tab(supervisor, payment.tab), loop, timeout=10)
        result["payment_tab_closed"] = True
        _forget(supervisor, payment.tab)
    except Exception as exc:  # health: allow BLE001 -- the status below decides what is reported
        logger.info("payment_done: closing the payment tab failed (%s)", type(exc).__name__)
        _drop_closed_tabs(supervisor)  # the close may have happened with its answer lost
    result["lock_released"] = _released(supervisor)
    if on_tab and result["lock_released"]:  # attaching installs the dialog bridge: never on a locked slot
        try:
            _wait(_follow(supervisor, result["new_tab"]), loop, timeout=10)
        except Exception as exc:  # health: allow BLE001 -- the next browser action re-attaches
            logger.info("payment_done: supervisor did not follow to the new tab (%s)", type(exc).__name__)
    if not result["lock_released"]:
        result["note"] = _UNPROVEN if result["payment_tab_closed"] else _CLOSE_FAILED
    return result
