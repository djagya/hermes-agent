"""D-1209 a (2026-11-27): a second press on the same Pay control is Hermes' decision, not prose.

Every Pay-like press booked in a lock record (``browser_payment_surface._book_press``) is kept here by identity: its
document (frame id, loader) and element (backendNodeId; its redacted caption when the node was not read). A Pay-like
press on a control already pressed in this record is refused (``payment_same_control``, nothing pressed) unless
payment_done answered ``pay_again: true`` after the latest Pay-like press and no Pay-like press was booked since. A
different control, or the same place on a new document (a new loader), is the checkout's next step and is not held.

payment_done sets ``pay_again`` only when all of these hold on its last look at the payment tab:

- no order was detected in this lock (``paid``), the latest Pay-like press opened no tab, its answer came back (not
  ``payment_click_uncertain``: that press is in ``presses`` but never ``answered``), and this process saw the whole
  lock (``known``);
- the tab's top document is the very document of that press (same frame and loader), its pressed element (read by
  backendNodeId) is still connected, rendered and enabled, and the top frame is not loading a navigation;
- the document shows failure wording or a field flagged invalid (``aria-invalid``, ``:user-invalid``) that it did not
  show right before that press, no strong order wording (a thank-you sentence, an order number), no order wording
  that was not there before the press, and is not busy (``readyState``, ``aria-busy``); the tree is the same after
  the reads.

A failed, slow or late read gives no ``pay_again``. Only booleans leave the page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_surface")

SAME_CONTROL = "payment_same_control"
# The one sentence every text about a second Pay press says (payment_done, the tool description).
AGAIN_RULE = ("Press the same Pay control again only when payment_done says pay_again: true; Hermes refuses it "
              "otherwise (payment_same_control).")
_SAME_CONTROL_TEXT = (
    "You already pressed this Pay control for this approval, so nothing was pressed. Call payment_done (before you "
    "change anything on the page): it says pay_again: true only when the page shows the payment failed or a field "
    "was flagged, with this control still there and no order. Otherwise ask the owner (Danil).")

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


@dataclass(frozen=True)
class Presses:
    """A lock record's Pay-like presses (``_LockGen.again``). ``keys``: ``(frame id, loader, backendNodeId or None,
    caption)`` of each; ``last``: the latest one's; ``answered``: ``pay_presses`` when a Pay-like press's answer last
    came back; ``granted``: ``(key, pay_presses)`` payment_done found ``pay_again`` for."""

    keys: frozenset = frozenset()
    last: Optional[tuple] = None
    answered: int = -1
    granted: Optional[tuple] = None


def _key(node: Any, caption: str) -> tuple:
    frame, loader, backend = (tuple(node or ()) + (None, None, None))[:3]
    return frame, loader, backend if type(backend) is int else None, caption


def _same(a: tuple, b: tuple) -> bool:
    """One control: the same document, and the same element (by caption when either node id is unread)."""
    if a[:2] != b[:2]:
        return False
    return a[2] == b[2] if a[2] is not None and b[2] is not None else a[3] == b[3]


def refusal(gen: Any, node: Any, caption: str) -> Optional[dict]:
    """``payment_same_control`` for a Pay-like press on a control already pressed in ``gen`` without a ``pay_again``
    since the latest Pay-like press, else None. Must hold ``browser_payment_surface._LOCK``."""
    presses, key = gen.again, _key(node, caption)
    if presses is None or not any(_same(k, key) for k in presses.keys):
        return None
    granted = presses.granted
    if granted is not None and granted[1] == gen.pay_presses and _same(granted[0], key):
        return None
    return {"success": False, "error_type": SAME_CONTROL, "error": _SAME_CONTROL_TEXT}


def book(gen: Any, node: Any, caption: str) -> None:
    """A Pay-like press booked on ``gen`` (after ``pay_presses`` counted it). Must hold ``_LOCK``."""
    presses, key = gen.again or Presses(), _key(node, caption)
    gen.again = replace(presses, keys=presses.keys | {key}, last=key)


def answered(gen: Any) -> None:
    """The latest Pay-like press's answer came back to the caller (``payment_click``). Must hold ``_LOCK``."""
    if gen is not None and gen.again is not None:
        gen.again = replace(gen.again, answered=gen.pay_presses)


def _eligible(gen: Any, row: tuple) -> Optional[tuple]:
    """``(key, pay_presses)`` when the lock record allows a ``pay_again`` on top document ``row`` at all (module
    docstring, first two points), else None. Must hold ``_LOCK``."""
    presses, base = gen.again, gen.pay_base or {}
    if presses is None or presses.last is None or not gen.known or gen.paid or gen.pay_tabs:
        return None
    last = presses.last
    if presses.answered != gen.pay_presses or tuple(last[:2]) != tuple(row[:2]) or last[2] is None:
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


async def signal(sup: Any, sid: str, row: tuple, scan: dict) -> Optional[bool]:
    """Failure wording or a flagged field on document ``row`` (``scan``: its wording scan); None when unread. Read
    right before every Pay-like press (``pay_base``): a ``pay_again`` needs it absent then."""
    found = await flags(sup, sid, row[0])
    return None if found is None else bool(scan["failure"] or found["invalid"])


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
        before = dict(gen.pay_base) if token is not None else {}
    if token is None or inflight(sup, gen):
        return {}
    scan = await scan_document(sup, sid, row)
    found = await flags(sup, sid, row[0]) if scan is not None else None
    if found is None or found["busy"] or scan["strong"] or not (scan["failure"] or found["invalid"]):
        return {}
    if scan["order"] and before.get("order") is not True:
        return {}
    if not await _pay_control_shown(sup, sid, row[0], token[0][2], fn=_PRESSABLE, unread=False):
        return {}
    if await sup._payment_tree(sid, child_sessions) != tree:
        return {}
    return {"pay_again": token}


def grant(sup: Any, token: Any) -> bool:
    """Book payment_done's ``pay_again`` (``probe``'s token) on the current lock record, when it still holds: the
    same record and latest Pay-like press, no press since, nothing loading. True when booked."""
    from tools import browser_payment_surface as surface
    from tools.browser_payment_detect import inflight

    if not isinstance(token, tuple):
        return False
    with surface._LOCK:
        gen = _record(sup)
    if gen is None or inflight(sup, gen):
        return False
    with surface._LOCK:
        if _record(sup) is not gen or _eligible(gen, token[0][:2]) != token:
            return False
        gen.again = replace(gen.again, granted=token)
    logger.info("payment_done: pay_again for the latest Pay press")
    return True
