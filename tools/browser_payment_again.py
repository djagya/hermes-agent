"""D-1209 a (2026-11-27): a second press on the same Pay control is Hermes' decision, not prose.

Every Pay-like press booked in a lock record (``browser_payment_surface._book_press``) is kept here by identity: its
document (frame id, loader), its element (backendNodeId) and its redacted caption. A Pay-like press on a control
already pressed in this record (the same document and the same element or the same caption) is refused
(``payment_same_control``, nothing pressed) unless payment_done answered ``pay_again: true`` after the latest Pay-like
press, no Pay-like press was booked since, and the page right before this press still holds what that answer saw:
the same document, no order wording, and no new tab, and (for an answer given on failure wording) the failure wording
still showing; an answer given on a flagged field alone survives the field being fixed. A different control, or the
same place on a new document (a new loader), is the checkout's next step and is not held.

payment_done sets ``pay_again`` only when all of these hold on its last look at the payment tab:

- no order was detected in this lock (``paid``), the tabs were read right before the latest Pay-like press and no tab
  was opened since (no new page target, none opened by a payment tab), its answer came back (not
  ``payment_click_uncertain``), and this process saw the whole lock (``known``);
- the tab's top document is the very document of that press (same frame and loader), its pressed element (read by
  backendNodeId) is still connected, rendered and enabled, and the top frame is not loading a navigation;
- the document shows failure wording or a field flagged invalid (``aria-invalid``, ``:user-invalid``) that it did not
  show right before that press, no strong order wording (a thank-you sentence, an order number), no order wording at
  all, and is not busy (``readyState``, ``aria-busy``); the tree is the same after the reads.

A failed, slow or late read gives no ``pay_again``. Only booleans leave the page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_surface")

SAME_CONTROL = "payment_same_control"
# The one sentence every text about a second Pay press says (payment_done, the tool description).
AGAIN_RULE = ("Press the same Pay control again only when payment_done says pay_again: true; Hermes refuses it "
              "otherwise (payment_same_control).")
_SAME_CONTROL_TEXT = (
    "You already pressed this Pay control for this approval, so nothing was pressed. Call payment_done (before you "
    "change anything on the page): it says pay_again: true only when the page shows the payment failed or a field "
    "was flagged, with this control still there and no order wording. Otherwise ask the owner (Danil).")

# The page's own flags (one boolean each): a field flagged invalid, and the document still busy.
FLAGS_JS = r"""(() => {
  const shown = (el) => el.getClientRects().length > 0;
  let marked;
  try { marked = document.querySelectorAll('[aria-invalid=true],:user-invalid'); }
  catch (e) { marked = document.querySelectorAll('[aria-invalid=true]'); }
  return {invalid: Array.from(marked).some(shown),
          busy: document.readyState !== 'complete' || Array.from(document.querySelectorAll('[aria-busy=true]')).some(shown)};
})()"""
# On the pressed element (resolved by its backendNodeId): connected, rendered and enabled.
_PRESSABLE = r"""function(){ if (!this.isConnected || this.disabled === true
    || (this.getAttribute && this.getAttribute('aria-disabled') === 'true')) return false;
  const r = this.getBoundingClientRect(); const st = getComputedStyle(this);
  return r.width >= 1 && r.height >= 1 && st.visibility === 'visible' && st.display !== 'none'; }"""


class Presses:
    """A lock record's Pay-like presses (``_LockGen.again``), read and written under ``_LOCK``. ``keys``: ``(frame
    id, loader, backendNodeId or None, caption)`` of each, the latest last; ``tabs``: the page targets right before
    the latest one (None: unread); ``answered``: its answer came back; ``granted``: the key payment_done found
    ``pay_again`` for (dropped by the next Pay-like press); ``wording``: that answer came from failure wording, not
    only from a flagged field."""

    __slots__ = ("answered", "granted", "keys", "tabs", "wording")

    def __init__(self) -> None:
        self.keys: list = []
        self.tabs: Optional[frozenset] = None
        self.answered = False
        self.granted: Optional[tuple] = None
        self.wording = False


def _key(node: Any, caption: str) -> tuple:
    frame, loader, backend = (tuple(node or ()) + (None, None, None))[:3]
    return frame, loader, backend if type(backend) is int else None, caption


def _same(a: tuple, b: tuple) -> bool:
    """One control: the same document, and the same element or the same caption (a re-mounted button is held too)."""
    return a[:2] == b[:2] and ((a[2] is not None and a[2] == b[2]) or a[3] == b[3])


def refusal(gen: Any, node: Any, caption: str, base: Optional[dict], tabs: Optional[frozenset]) -> Optional[dict]:
    """``payment_same_control`` for a Pay-like press on a control already pressed in ``gen``, else None. A
    ``pay_again`` for it counts only while the look right before this press (``base``, ``tabs``) still shows that
    document with no order wording and no new tab, and the failure still showing when the answer came from failure
    wording (a flag-only answer survives the field being fixed). Must hold
    ``browser_payment_surface._LOCK``."""
    presses, key = gen.again, _key(node, caption)
    if presses is None or not any(_same(k, key) for k in presses.keys):
        return None
    granted, base = presses.granted, base or {}
    if (granted is not None and _same(granted, key) and tuple(base.get("doc") or ()) == granted[:2]
            and (base.get("signal") is True or not presses.wording) and base.get("strong") is False and base.get("order") is False
            and tabs is not None and presses.tabs is not None and tabs <= presses.tabs):
        return None
    return {"success": False, "error_type": SAME_CONTROL, "error": _SAME_CONTROL_TEXT}


def book(gen: Any, node: Any, caption: str, tabs: Optional[frozenset]) -> None:
    """A Pay-like press booked on ``gen`` (``tabs``: the page targets right before it). Must hold ``_LOCK``."""
    presses = gen.again = gen.again or Presses()
    presses.keys.append(_key(node, caption))
    presses.tabs, presses.answered, presses.granted = tabs, False, None


def answered(gen: Any) -> None:
    """The latest Pay-like press's answer came back to the caller (``payment_click``). Must hold ``_LOCK``."""
    if gen is not None and gen.again is not None:
        gen.again.answered = True


def _eligible(gen: Any, row: tuple) -> Optional[tuple]:
    """``(key, pay_presses)`` when the lock record allows a ``pay_again`` on top document ``row`` at all (module
    docstring, first two points), else None. Must hold ``_LOCK``."""
    presses, base = gen.again, gen.pay_base or {}
    if presses is None or not presses.keys or not presses.answered or presses.tabs is None:
        return None
    last = presses.keys[-1]
    if not gen.known or gen.paid or gen.pay_tabs or last[:2] != tuple(row[:2]) or last[2] is None:
        return None
    if tuple(base.get("doc") or ()) != tuple(row[:2]) or base.get("signal") is not False:
        return None
    return last, gen.pay_presses


def _record(sup: Any) -> Any:
    from tools import browser_payment_surface as surface

    return surface._GENS.get(sup) if sup in surface._HELD else None


async def flags(sup: Any, sid: str, frame_id: str) -> Optional[dict]:
    """``{"invalid", "busy": bool}`` from ``FLAGS_JS`` in a fresh isolated world of ``frame_id``, or None (unread)."""
    try:
        ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "hermes-payment-done"},
                              session_id=sid))["result"]["executionContextId"]
        reply = (await sup._cdp("Runtime.evaluate", {"expression": FLAGS_JS, "contextId": ctx, "returnByValue": True},
                                session_id=sid, timeout=10))["result"]
    except Exception as exc:  # health: allow BLE001 -- unread: no pay_again (fails closed)
        logger.info("payment: page flags not read (%s)", type(exc).__name__)
        return None
    value = (reply.get("result") or {}).get("value") if not reply.get("exceptionDetails") else None
    if not (isinstance(value, dict) and set(value) == {"invalid", "busy"} and all(type(v) is bool for v in value.values())):
        return None
    return value


async def signal(sup: Any, sid: str, frame_id: str, failure: bool, bound: float) -> Optional[bool]:
    """Failure wording (``failure``, the wording scan's) or a flagged field on the document; None when unread or
    slower than ``bound``. Read right before every Pay-like press (``pay_base``), once no order evidence was found."""
    if failure:
        return True
    try:
        found = await asyncio.wait_for(flags(sup, sid, frame_id), bound)
    except Exception as exc:  # health: allow BLE001 -- slow: unread, no pay_again later (fails closed)
        logger.info("payment: page flags before the press not read (%s)", type(exc).__name__)
        return None
    return None if found is None else found["invalid"]


async def _no_new_tab(sup: Any, before: frozenset) -> bool:
    """No page target that was not there right before the press, and none a payment tab opened."""
    from tools.browser_payment_done import payment_tabs

    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    opener = payment_tabs(sup)
    return all(t.get("targetId") in before and t.get("openerId") not in opener
               for t in infos if isinstance(t, dict) and t.get("type") == "page")


async def probe(sup: Any, sid: str, tree: tuple, child_sessions: dict) -> dict:
    """payment_done's look at the payment tab's top document: ``{"pay_again": token}`` when it allows one (module
    docstring), else ``{}``. The token is checked again by ``grant``."""
    from tools import browser_payment_surface as surface
    from tools.browser_payment_detect import inflight
    from tools.browser_payment_order_evidence import _pay_control_shown, scan_document

    row = tree[0]
    with surface._LOCK:
        gen = _record(sup)
        token = _eligible(gen, row) if gen is not None else None
        tabs = gen.again.tabs if token is not None else None
    if token is None or inflight(sup, gen):
        return {}
    scan = await scan_document(sup, sid, row)
    found = await flags(sup, sid, row[0]) if scan is not None else None
    if found is None or found["busy"] or scan["strong"] or scan["order"] or not (scan["failure"] or found["invalid"]):
        return {}
    if not await _no_new_tab(sup, tabs):
        return {}
    if await _pay_control_shown(sup, sid, row[0], token[0][2], fn=_PRESSABLE) is not True:
        return {}
    if await sup._payment_tree(sid, child_sessions) != tree:
        return {}
    return {"pay_again": (*token, bool(scan["failure"]))}


def grant(sup: Any, token: Any) -> bool:
    """Book payment_done's ``pay_again`` (``probe``'s token) on the current lock record, when it still holds: the
    same record and latest Pay-like press, no press since, nothing loading. True when booked."""
    from tools import browser_payment_surface as surface
    from tools.browser_payment_detect import inflight

    if not (isinstance(token, tuple) and len(token) == 3):
        return False
    with surface._LOCK:
        gen = _record(sup)
    if gen is None or inflight(sup, gen):
        return False
    with surface._LOCK:
        if _record(sup) is not gen or _eligible(gen, token[0][:2]) != token[:2]:
            return False
        gen.again.granted, gen.again.wording = token[0], token[2]
    logger.info("payment_done: pay_again for the latest Pay press")
    return True
