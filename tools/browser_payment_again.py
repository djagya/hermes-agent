"""D-1209 a (2026-11-27): a second press on the same Pay control is Hermes' decision, not prose.

Every Pay-like press booked in a lock record (``browser_payment_surface._book_press``) is kept by identity: its
document (frame id, loader), its element (backendNodeId) and its redacted caption. A Pay-like press on a control
already pressed in this record (the same document and the same element or the same caption) is refused
(``payment_same_control``, nothing pressed) unless the last payment_done said ``pay_again: true`` for the latest
Pay-like press and ``allows`` holds on the look right before this press. A press whose element id was not read is
the same control as any earlier one on its document (fails closed). Another control, or the same place on a new
document (a new loader), is the checkout's next step and is not held. A confirm accepted with payment_dialog counts as
one more press of the latest Pay control (``accepted``).

Only a field the form flagged invalid (``aria-invalid``, ``:user-invalid``) after the press, not before it, gives
``pay_again`` (D-1260, built as a); page text only ever vetoes. One rule of vetoes (``allows``) judges one look (``look``) at both
moments, payment_done's last look and the look right before the press: the pressed top document, no order wording
(strong or not), no money-taken wording (``TAKEN``: charged, abgebucht, erfolgt ...), no pending wording (``PENDING``:
a bank app to approve, "bitte warten"), not busy (``readyState``, ``aria-busy``), the pressed element connected,
rendered and enabled, no page target new since the latest Pay-like press or opened by a payment tab. At the press the
flag need not show any more (Sera fixed the field, D-0961).

payment_done drops the stored answer when it starts (``clear``, which numbers the call) and stores one only at its
not-reached exit (``grant``), when no later payment_done started meanwhile, from its own last look, when also: no order
detected (``paid``) and no tab a press opened in this lock, the latest press answered (not
``payment_click_uncertain``) and seen whole by this process (``known``), no flag right before that press, nothing
loading, the tree the same after the reads. A failed, slow or late read gives none. Only booleans leave the page.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_surface")

SAME_CONTROL = "payment_same_control"
# The one sentence every text about a second Pay press says (payment_done, the tool description). Hermes holds the
# same control on the same page; a second purchase button or a reloaded page is the sentence's word only.
AGAIN_RULE = ("After a Pay press, the same Pay control on the same page goes again only after payment_done says "
              "pay_again: true (Hermes refuses it otherwise: payment_same_control); press a second purchase button or "
              "the same order's Pay on a reloaded page only when payment_done says pay_again: true; the checkout's next "
              "step (a different control such as a confirm or buy step) is pressed as usual.")
_SAME_CONTROL_TEXT = (
    "You already pressed this Pay control for this approval, so nothing was pressed. Call payment_done (before you "
    "change anything on the page): it says pay_again: true only for a field the form flagged after the press, with "
    "this control still there and no order, payment-taken or pending wording. Otherwise ask the owner (Danil): his new "
    "approval (a new card fill approval) covers one more Pay press.")

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
    """A lock record's Pay-like presses (``_LockGen.again``), under ``_LOCK``. ``keys``: ``(frame id, loader,
    backendNodeId or None, caption)`` of each, the latest last (a confirm accepted after it repeats it); ``tabs``: the
    look's tabs right before the latest one; ``booked``: ``len(keys)`` at its booking; ``answered``: its answer came
    back; ``granted``: the last payment_done's ``pay_again`` (its key); ``calls``: payment_done calls started
    (``clear``)."""

    __slots__ = ("answered", "booked", "calls", "granted", "keys", "tabs")

    def __init__(self) -> None:
        self.keys: list = []
        self.tabs: Optional[frozenset] = None
        self.answered = False
        self.booked = 0
        self.granted: Optional[tuple] = None
        self.calls = 0


def _key(node: Any, caption: str) -> tuple:
    frame, loader, backend = (tuple(node or ()) + (None, None, None))[:3]
    return frame, loader, backend if type(backend) is int else None, caption


def _same(a: tuple, b: tuple) -> bool:
    """One control: the same document, and the same element, an element not read on either side (fails closed) or the
    same caption (a re-mounted button is held too)."""
    return a[:2] == b[:2] and (a[2] is None or b[2] is None or a[2] == b[2] or a[3] == b[3])


def allows(seen: Optional[dict], doc: tuple, tabs: Optional[frozenset]) -> bool:
    """The vetoes (module docstring) on ``seen`` (``look``'s), all clear: document ``doc``, no page target outside
    ``tabs`` (those right before the latest Pay-like press)."""
    seen = seen or {}
    now = seen.get("tabs")
    return (tuple(seen.get("doc") or ()) == tuple(doc) and seen.get("pressable") is True
            and all(seen.get(k) is False for k in ("strong", "order", "taken", "pending", "busy"))
            and now is not None and tabs is not None and now <= tabs)


async def look(sup: Any, sid: str, seen: dict, backend: Any) -> dict:
    """``seen`` (``doc``: frame id, loader; the wording scan's booleans) with ``FLAGS_JS``'s
    ``invalid`` and ``busy``, ``pressable`` (element ``backend``) and ``tabs`` (the page targets; None when a payment
    tab opened one). Nothing is added after an unread scan (``strong`` None) or read (``allows``: False)."""
    from tools.browser_payment_done import payment_tabs
    from tools.browser_payment_order_evidence import _pay_control_shown

    if seen.get("strong") is None:
        return seen
    try:
        ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": seen["doc"][0], "worldName": "hermes-payment-done"},
                              session_id=sid))["result"]["executionContextId"]
        reply = (await sup._cdp("Runtime.evaluate", {"expression": FLAGS_JS, "contextId": ctx, "returnByValue": True},
                                session_id=sid, timeout=10))["result"]
        found = (reply.get("result") or {}).get("value") if not reply.get("exceptionDetails") else None
        if not (isinstance(found, dict) and set(found) == {"invalid", "busy"} and all(type(v) is bool for v in found.values())):
            return seen
        pressable = await _pay_control_shown(sup, sid, seen["doc"][0], backend, fn=_PRESSABLE) if type(backend) is int else None
        pages = [t for t in (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
                 if isinstance(t, dict) and t.get("type") == "page"]
    except Exception as exc:  # health: allow BLE001 -- unread: no pay_again, the same control refused (fails closed)
        logger.info("payment: page flags or tabs not read (%s)", type(exc).__name__)
        return seen
    opened = any(t.get("openerId") in payment_tabs(sup) for t in pages)
    return {**seen, **found, "pressable": pressable, "tabs": None if opened else frozenset(t.get("targetId") for t in pages)}


def refusal(gen: Any, node: Any, caption: str, seen: Optional[dict]) -> Optional[dict]:
    """``payment_same_control`` for a Pay-like press on a control already pressed in ``gen`` unless the stored
    ``pay_again`` is for it and ``allows`` holds on ``seen`` (the look right before this press). Must hold ``_LOCK``."""
    presses, key = gen.again, _key(node, caption)
    if presses is None or not any(_same(k, key) for k in presses.keys):
        return None
    granted = presses.granted
    if granted is not None and _same(granted, key) and allows(seen, granted[:2], presses.tabs):
        return None
    return {"success": False, "error_type": SAME_CONTROL, "error": _SAME_CONTROL_TEXT}


def book(gen: Any, node: Any, caption: str, seen: Optional[dict]) -> None:
    """A Pay-like press booked on ``gen`` (``seen``: the look right before it). Must hold ``_LOCK``."""
    presses = gen.again = gen.again or Presses()
    presses.keys.append(_key(node, caption))
    presses.tabs, presses.answered, presses.granted = (seen or {}).get("tabs"), False, None
    presses.booked = len(presses.keys)


def answered(gen: Any) -> None:
    """The latest Pay-like press's answer came back to the caller (``payment_click``), unless a confirm was accepted
    after its booking (``accepted``: its answer says nothing about that one). Must hold ``_LOCK``."""
    if gen is not None and gen.again is not None and len(gen.again.keys) == gen.again.booked:
        gen.again.answered = True


def _eligible(gen: Any, row: tuple) -> Optional[tuple]:
    """``(key, presses so far)`` when the lock record allows a ``pay_again`` on top document ``row`` at all (module
    docstring, last paragraph), else None. Must hold ``_LOCK``."""
    presses, base = gen.again, gen.pay_base or {}
    last = presses.keys[-1] if presses is not None and presses.keys else None
    if (last is None or not presses.answered or presses.tabs is None or not gen.known or gen.paid or gen.pay_tabs
            or last[2] is None or not last[:2] == tuple(base.get("doc") or ()) == tuple(row[:2])
            or base.get("invalid") is not False):
        return None
    return last, len(presses.keys)


def _record(sup: Any) -> Any:
    from tools import browser_payment_surface as surface

    return surface._GENS.get(sup) if sup in surface._HELD else None


def clear(sup: Any) -> Optional[tuple]:
    """payment_done's entry: the stored ``pay_again`` is dropped (only this call's not-reached exit stores one).
    Returns this call's number (``grant``'s ``call``), None when no Pay-like press was booked yet."""
    from tools import browser_payment_surface as surface

    with surface._LOCK:
        gen = _record(sup)
        if gen is None or gen.again is None:
            return None
        gen.again.granted = None
        gen.again.calls += 1
        return gen.again, gen.again.calls


def accepted(sup: Any) -> None:
    """payment_dialog is about to accept a confirm on the shop's page: it may place the order as a Pay press does, so
    it counts as one more press of the latest Pay-like control (the stored ``pay_again`` dropped, and no new one until
    a Pay press is answered again: the page's wording from before the accept says nothing about it)."""
    from tools import browser_payment_surface as surface

    with surface._LOCK:
        gen = _record(sup)
        if gen is not None and gen.again is not None and gen.again.keys:
            gen.again.keys.append(gen.again.keys[-1])
            gen.again.granted, gen.again.answered = None, False


async def probe(sup: Any, sid: str, tree: tuple, child_sessions: dict) -> dict:
    """payment_done's look at the payment tab's top document: ``{"pay_again": token}`` when it shows a flagged field,
    ``allows`` holds on it and the lock record allows one, else ``{}``. The token is checked again by ``grant``."""
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
    if seen.get("invalid") is not True or not allows(seen, row[:2], tabs) or await sup._payment_tree(
            sid, child_sessions) != tree:
        return {}
    return {"pay_again": token}


def grant(sup: Any, token: Any, call: Optional[tuple]) -> bool:
    """payment_done's not-reached exit: store ``probe``'s token when it still holds (the same record and latest
    Pay-like press, no press since, nothing loading) and ``call`` (``clear``'s) is the latest payment_done started,
    else store "no": an earlier call that ends last never stores its yes. True when stored."""
    from tools import browser_payment_surface as surface
    from tools.browser_payment_detect import inflight

    with surface._LOCK:
        gen = _record(sup)
    if gen is None:
        return False
    holds = token is not None and not inflight(sup, gen)
    with surface._LOCK:
        if _record(sup) is not gen:
            return False
        latest = call is not None and call[0] is gen.again and call[0].calls == call[1]
        if not holds or not latest or _eligible(gen, token[0][:2]) != token:
            if gen.again is not None:
                gen.again.granted = None
            return False
        gen.again.granted = token[0]
    logger.info("payment_done: pay_again for the latest Pay press")
    return True
