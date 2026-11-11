"""Native "payment done" handover (payment lockdown design, section 2).

Once a card fill claims the payment surface, browser-control locks the slot for every agent
connection until each owning payment tab is destroyed. ``browser_vault action=payment_done`` is the
runtime's way out after the Pay click, on the supervisor's own (native) socket:

1. Wait, bounded, for the payment tab's top document to be a new document on the shop's origin
   (the checkout's merchant, or ``expected_origin``) that is not a payment-provider or approved
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
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from agent.vault_payment_policy import (
    PAYMENT_PROVIDERS,
    UNSUPPORTED_PROVIDERS,
    PaymentPolicyError,
    https_origin,
    provider_for_origin,
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
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


@dataclass(frozen=True)
class _Payment:
    """What a confirmed claim recorded about one payment tab (no value, only page structure)."""

    tab: str  # the page target holding the checkout
    merchant: str
    loader: str  # the top document's loader when the card was claimed
    documents: frozenset  # (frame id, loader) of each card document
    receiving: frozenset  # non-merchant origins that received card fields (reviewed or owner-approved)
    paths: frozenset  # (origin, path) of those card documents
    merchant_fields: bool = False  # the merchant document itself received card fields

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
        urls = {row[0]: row[2] for row in tree}
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
            paths=frozenset((d.origin, urlsplit(urls.get(d.frame_id, "")).path) for d in hosted),
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
                               old.receiving | payment.receiving, old.paths | payment.paths,
                               old.merchant_fields or payment.merchant_fields)
        tabs[payment.tab] = payment


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


def _card_frame(row: tuple, payment: _Payment) -> bool:
    """A frame that is (or can be) a card document: one of this payment's own card documents, a
    document at the same origin and path as one of them, or a provider's declared card-field path.
    A provider's other frames (Stripe.js's hidden controller, for one) hold no card field."""
    fid, loader, url, origin = row[0], row[1], row[2], row[3]
    if (fid, loader) in payment.documents:
        return True
    path = urlsplit(url).path if isinstance(url, str) else ""
    for o in {origin, _url_origin(url)} - {""}:
        if (o, path) in payment.paths:
            return True
        provider = provider_for_origin(o)
        if provider is not None and provider.paths and provider.admits(url):
            return True
    return False


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
  let cardFields = 0;
  for (const el of document.querySelectorAll('input, select')) {
    const ac = String(el.getAttribute('autocomplete') || '');
    const named = String(el.getAttribute('name') || '') + ' ' + String(el.getAttribute('id') || '');
    if (CARD_AC.test(ac) || CARD_NAME.test(named)) cardFields++;
  }
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
                      ("MAX_TITLE", MAX_TITLE), ("MAX_LINK_URL", MAX_LINK_URL)):
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
                "url shows the site the tab is on (origin only while locked). If a bank check is waiting, ask the user to finish it, then "
                "call payment_done again; if the confirmation is on another site, call payment_done with "
                "expected_origin set to that site's origin; otherwise ask the user.")
_TAB_GONE = ("The payment tab is no longer open, so there is no confirmation page to read. Nothing was done. "
             "lock_released says whether browser control has unlocked the browser; check the order with the "
             "shop or the user's email.")
_NO_NEW_TAB = ("The confirmation was read, but a new tab could not be opened, so the payment tab was left open and "
               "the browser stays locked. Use text and links; close the payment tab or ask the user to end the lock.")
_CLOSE_FAILED = ("The confirmation was read and a new tab opened, but closing the payment tab failed, and browser "
                 "control has not confirmed the unlock. Close the payment tab or ask the user.")
_UNPROVEN = ("The confirmation was read, the new tab opened and the payment tab closed, but browser control has not "
             "yet confirmed the unlock. Use text and links; if browser actions are still refused, ask the user.")


def _payment_for(supervisor: Any) -> tuple[Optional[_Payment], Optional[dict]]:
    with _LOCK:
        tabs = dict(_PAYMENTS.get(supervisor) or {})
    if not tabs:
        return None, _refusal("payment_done_no_payment", _NO_PAYMENT)
    if len(tabs) > 1:
        return None, _refusal("payment_done_ambiguous", _AMBIGUOUS)
    return next(iter(tabs.values())), None


def _expected(payment: _Payment, expected_origin: str) -> tuple[str, Optional[dict]]:
    if expected_origin:
        try:
            expected = https_origin(expected_origin)
        except PaymentPolicyError:
            return "", _refusal("payment_done_origin_invalid", _ORIGIN_INVALID)
        if expected in _card_origins(payment):
            return "", _refusal("payment_done_origin_invalid", _ORIGIN_INVALID)
        return expected, None
    if payment.merchant in _card_origins(payment):
        return "", _refusal("payment_done_origin_invalid", _ORIGIN_REQUIRED)
    return payment.merchant, None


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
    """Close the payment tab; returns whether the supervisor's page session was on it."""
    with sup._state_lock:
        sid = sup._page_session_id if sup._active else None
    on_tab = False
    if sid:
        try:
            info = (await sup._cdp("Target.getTargetInfo", session_id=sid))["result"]["targetInfo"]
            on_tab = info.get("targetId") == tab
        except Exception:  # health: allow BLE001 -- only decides whether to follow to the new tab
            on_tab = False
    await sup._cdp("Target.closeTarget", {"targetId": tab})
    return on_tab


async def _follow(sup: Any, new_tab: str) -> None:
    """Best effort: point the supervisor at the new tab once its page (the payment tab) is closed."""
    with sup._state_lock:
        old = sup._page_session_id
    sid = await sup._attach_page(new_tab, timeout=5)
    with sup._state_lock:
        if sup._page_session_id == old:
            sup._page_session_id = sid


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


def payment_done(supervisor: Any, expected_origin: str = "") -> dict:
    """``browser_vault action=payment_done``: see the module docstring."""
    loop = getattr(supervisor, "_loop", None)
    if loop is None or not loop.is_running():
        return _refusal("supervisor_required", "The supervised browser is not attached; nothing was done.")
    payment, refused = _payment_for(supervisor)
    if refused:
        return refused
    expected, refused = _expected(payment, expected_origin)
    if refused:
        return refused
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
        if state == "ready":
            page = info
            break
        previous = key if state == "settling" else None
        time.sleep(max(0.0, min(_POLL_S, deadline - time.monotonic())))
    if page is None:
        # The tab is still locked: like browser-control for every agent connection, report its site only
        # (a locked page's title and path could carry what a planted script copied there).
        return _refusal("payment_confirmation_not_reached", _NOT_REACHED, url=_url_origin(seen.get("url", "")),
                        title="", lock_released=False)
    context = page.pop("browser_context", None)
    result = {"success": True, **page, "new_tab": None, "payment_tab_closed": False, "lock_released": False}
    try:  # the new tab first: the payment tab is closed only once the confirmation has somewhere to go
        result["new_tab"] = _wait(_open_tab(supervisor, page["url"], context), loop, timeout=15)
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
    result["lock_released"] = _released(supervisor)
    if on_tab:
        try:
            _wait(_follow(supervisor, result["new_tab"]), loop, timeout=10)
        except Exception as exc:  # health: allow BLE001 -- the next browser action re-attaches
            logger.info("payment_done: supervisor did not follow to the new tab (%s)", type(exc).__name__)
    if not result["lock_released"]:
        result["note"] = _UNPROVEN if result["payment_tab_closed"] else _CLOSE_FAILED
    return result
