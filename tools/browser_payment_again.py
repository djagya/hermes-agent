"""D-1209 a (2026-11-27): a second press on the same Pay control is Hermes' decision, not prose.

Every Pay-like press booked in a lock record (``browser_payment_surface._book_press``) is kept here by identity: its
document (frame id, loader), its element (backendNodeId) and its redacted caption. A Pay-like press on a control
already pressed in this record (the same document and the same element or the same caption) is refused
(``payment_same_control``, nothing pressed) unless the last payment_done answered ``pay_again: true`` for the latest
Pay-like press, no Pay-like press was booked since, and ``allows`` holds on the look right before this press. A
different control, or the same place on a new document (a new loader), is the checkout's next step and is not held.

One rule (``allows``) judges one look (``look``) at both moments: payment_done's last look before its
``payment_confirmation_not_reached`` answer, and the look right before the press. It holds when the top document is
the pressed one, it shows no strong order wording (a thank-you sentence, an order number) and no order wording at
all, it is not busy (``readyState``, ``aria-busy``), the pressed element (read by backendNodeId) is connected,
rendered and enabled, and no page target exists that was not there right before the latest Pay-like press (and none a
payment tab opened); plus failure wording or a field flagged invalid (``aria-invalid``, ``:user-invalid``) at
payment_done. At the press an answer given on failure wording needs it still showing; one given on a flagged field
alone survives the field being fixed (D-0961).

payment_done drops the stored answer when it starts and stores one only at its not-reached exit, from its own last
look, when also: no order was detected in this lock (``paid``), no tab a press opened was recorded, the latest press's
answer came back (not ``payment_click_uncertain``), this process saw the whole lock (``known``), the page showed
neither failure wording nor a flag right before that press, nothing is loading, and the tree is the same after the
reads. Any other exit, a failed, slow or late read, gives none. Only booleans leave the page.
"""

from __future__ import annotations

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
    the latest one (``look``'s; None: unread); ``answered``: its answer came back; ``granted``: the key the last
    payment_done found ``pay_again`` for (dropped by the next payment_done or Pay-like press); ``wording``: that
    answer came from failure wording, not only from a flagged field."""

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


def allows(seen: Optional[dict], doc: tuple, tabs: Optional[frozenset], wording: Optional[bool] = None) -> bool:
    """The one rule (module docstring) on ``seen`` (``look``'s): document ``doc``, no page target outside ``tabs``
    (those right before the latest Pay-like press). ``wording``: None at payment_done (failure wording or a flagged
    field must show); at the press the stored answer's: True needs the failure wording still showing, False (a
    flagged field alone) nothing more."""
    seen = seen or {}
    now = seen.get("tabs")
    if (tuple(seen.get("doc") or ()) != tuple(doc) or seen.get("strong") is not False or seen.get("order") is not False
            or seen.get("busy") is not False or seen.get("pressable") is not True
            or now is None or tabs is None or not now <= tabs):
        return False
    if seen.get("failure") is True:
        return True
    return seen.get("invalid") is True if wording is None else not wording


async def look(sup: Any, sid: str, seen: dict, backend: Any) -> dict:
    """``seen`` (``{"doc": (frame id, loader)}`` and the wording scan's ``strong``, ``order``, ``failure``) with what
    ``allows`` reads next on that document: ``FLAGS_JS``'s ``invalid`` and ``busy``, ``pressable`` (element
    ``backend``) and ``tabs`` (``_tabs``). Nothing more is read when the scan is unread (``strong`` None) or the flags
    are; a part unread stays out or None (``allows``: False)."""
    from tools.browser_payment_order_evidence import _pay_control_shown

    if seen.get("strong") is None or (found := await flags(sup, sid, seen["doc"][0])) is None:
        return seen
    pressable = (await _pay_control_shown(sup, sid, seen["doc"][0], backend, fn=_PRESSABLE)
                 if type(backend) is int else None)
    return {**seen, **found, "pressable": pressable, "tabs": await _tabs(sup)}


def refusal(gen: Any, node: Any, caption: str, seen: Optional[dict]) -> Optional[dict]:
    """``payment_same_control`` for a Pay-like press on a control already pressed in ``gen``, else None: the stored
    ``pay_again`` is for this control and ``allows`` holds on ``seen`` (the look right before this press). Must hold
    ``browser_payment_surface._LOCK``."""
    presses, key = gen.again, _key(node, caption)
    if presses is None or not any(_same(k, key) for k in presses.keys):
        return None
    granted = presses.granted
    if granted is not None and _same(granted, key) and allows(seen, granted[:2], presses.tabs, presses.wording):
        return None
    return {"success": False, "error_type": SAME_CONTROL, "error": _SAME_CONTROL_TEXT}


def book(gen: Any, node: Any, caption: str, seen: Optional[dict]) -> None:
    """A Pay-like press booked on ``gen`` (``seen``: the look right before it). Must hold ``_LOCK``."""
    presses = gen.again = gen.again or Presses()
    presses.keys.append(_key(node, caption))
    presses.tabs, presses.answered, presses.granted = (seen or {}).get("tabs"), False, None


def answered(gen: Any) -> None:
    """The latest Pay-like press's answer came back to the caller (``payment_click``). Must hold ``_LOCK``."""
    if gen is not None and gen.again is not None:
        gen.again.answered = True


def _eligible(gen: Any, row: tuple) -> Optional[tuple]:
    """``(key, pay_presses)`` when the lock record allows a ``pay_again`` on top document ``row`` at all (module
    docstring, last paragraph), else None. Must hold ``_LOCK``."""
    presses, base = gen.again, gen.pay_base or {}
    if presses is None or not presses.keys or not presses.answered or presses.tabs is None:
        return None
    last = presses.keys[-1]
    if not gen.known or gen.paid or gen.pay_tabs or last[:2] != tuple(row[:2]) or last[2] is None:
        return None
    if tuple(base.get("doc") or ()) != tuple(row[:2]) or base.get("failure") is not False \
            or base.get("invalid") is not False:
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


async def _tabs(sup: Any) -> Optional[frozenset]:
    """The page targets now; None when unread or when a payment tab opened one (a bank's or a confirmation tab)."""
    from tools.browser_payment_done import payment_tabs

    try:
        infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    except Exception as exc:  # health: allow BLE001 -- unread: no pay_again (fails closed)
        logger.info("payment: tabs not read (%s)", type(exc).__name__)
        return None
    pages = [t for t in infos if isinstance(t, dict) and t.get("type") == "page"]
    opener = payment_tabs(sup)
    return None if any(t.get("openerId") in opener for t in pages) else frozenset(t.get("targetId") for t in pages)


def clear(sup: Any) -> None:
    """payment_done's entry: the stored ``pay_again`` is dropped (only this call's not-reached exit stores one)."""
    from tools import browser_payment_surface as surface

    with surface._LOCK:
        gen = _record(sup)
        if gen is not None and gen.again is not None:
            gen.again.granted = None


async def probe(sup: Any, sid: str, tree: tuple, child_sessions: dict) -> dict:
    """payment_done's look at the payment tab's top document: ``{"pay_again": token}`` when ``allows`` holds on it and
    the lock record allows one, else ``{}``. The token is checked again by ``grant``."""
    from tools import browser_payment_surface as surface
    from tools.browser_payment_detect import inflight
    from tools.browser_payment_order_evidence import scan_document

    row = tree[0]
    with surface._LOCK:
        gen = _record(sup)
        token = _eligible(gen, row) if gen is not None else None
        tabs = gen.again.tabs if token is not None else None
    if token is None or inflight(sup, gen):
        return {}
    seen = await look(sup, sid, {"doc": tuple(row[:2]), "strong": None, **(await scan_document(sup, sid, row) or {})},
                      token[0][2])
    if not allows(seen, row[:2], tabs) or await sup._payment_tree(sid, child_sessions) != tree:
        return {}
    return {"pay_again": (*token, seen["failure"] is True)}


def grant(sup: Any, token: Any) -> bool:
    """payment_done's not-reached exit: store its last look's ``pay_again`` (``probe``'s token) when it still holds
    (the same record and latest Pay-like press, no press since, nothing loading), else store none. True when stored."""
    from tools import browser_payment_surface as surface
    from tools.browser_payment_detect import inflight

    with surface._LOCK:
        gen = _record(sup)
    ok = isinstance(token, tuple) and len(token) == 3 and gen is not None and not inflight(sup, gen)
    with surface._LOCK:
        if gen is None or _record(sup) is not gen or gen.again is None:
            return False
        ok = ok and _eligible(gen, token[0][:2]) == token[:2]
        gen.again.granted, gen.again.wording = (token[0], token[2]) if ok else (None, False)
    if ok:
        logger.info("payment_done: pay_again for the latest Pay press")
    return ok
