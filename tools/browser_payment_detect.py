"""Payment detection instead of a press budget (build G1; owner direction 2026-11-19: "find a way of detecting the
payment and allowing the pages to do stuff pretty freely ... not limiting pages with '1 click'").

While an approval is open (the admission holds; an approved total is read before every press, as before) every press
goes through, Pay-like or not, any number of times. After a Pay-like press (``browser_payment_press_kind._pay_press``)
the value-free order-evidence scan (``browser_payment_order_evidence``) runs on the payment tab's top document, and
on a tab that press opened. Order evidence on a shop page spends the approval (``_LockGen.paid``): every later Pay-like
press under it is refused (``payment_already_made``). The same look runs right before a later Pay-like press, so a
confirmation that loaded after the last answer is not pressed past.

Only one thing else refuses a Pay-like press: the in-flight guard. While the top frame is still loading a navigation
that started after this approval's last Pay-like press (Chrome's own navigation-start and stop events,
``loading_since``; same-document navigations ignored), for at most ``_INFLIGHT_S``, the press would land on a
checkout whose order is still being placed (``payment_in_flight``).

Evidence counts only on a shop page: a document on the merchant's site (not another site, such as a bank's 3-D Secure
page, and not a payment provider's or card origin). A checkout-like document needs order wording and no enabled
purchase button; any other shop document needs order wording too. Failure wording and the pressed Pay control still
shown are never evidence. Only fixed codes and booleans leave the page.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_surface")

_INFLIGHT_S = 20.0  # the longest a Pay-like press's navigation holds the next Pay-like press back
_BEFORE_SCAN_S = 6.0  # the longest the look before a Pay-like press may take (unread: the press goes on)
_MAX_NEW_TABS = 2

SPENT = "payment_already_made"
INFLIGHT = "payment_in_flight"
_INFLIGHT_TEXT = (
    "The page is still processing the last Pay press (its navigation has not finished loading), so nothing was "
    "pressed. Wait and read it again (payment_controls): an order confirmation means payment_done; another step "
    "(a confirm, 3-D Secure, a dialog) can be pressed once the page has loaded.")
_EVIDENCE_TEXT = {"confirmation_text": "the checkout page shows order wording and no purchase button",
                  "new_document": "a shop page shows order wording"}


def spent_refusal(gen: Any) -> Optional[dict]:
    """``payment_already_made`` once a payment was detected for this approval (``gen``: its lock record), else None.
    Callers outside ``_LOCK`` read a bool that only ever goes from False to True; ``_book_press`` checks it again."""
    if gen is None or not gen.paid:
        return None
    by = gen.paid_by or {}
    where = " in a new tab" if by.get("where") == "new_tab" else ""
    caption = by.get("caption") or "a Pay-like control"
    why = _EVIDENCE_TEXT.get(by.get("evidence"), "order evidence")
    return {"success": False, "error_type": SPENT,
            "error": (f"A payment was detected for this approval (after the press on \"{caption}\": {why}{where}), so "
                      "nothing was pressed. Call payment_done; a new purchase needs a new approval from the owner "
                      "(Danil).")}


def detected(supervisor: Any) -> Optional[dict]:
    """The detection that spent this supervisor's current lock (``where``: ``top`` with ``frame``/``loader``, or
    ``new_tab`` with ``tab``; ``evidence``), or None."""
    from tools import browser_payment_surface as surface

    with surface._LOCK:
        gen = surface._GENS.get(supervisor) if supervisor in surface._HELD else None
        return dict(gen.paid_by) if gen is not None and gen.paid and gen.paid_by else None


def inflight(sup: Any, gen: Any) -> bool:
    """The top frame is still loading a navigation that started after this approval's last Pay-like press, within
    ``_INFLIGHT_S`` of that press. No record (a supervisor without the events): False."""
    at = getattr(gen, "pay_at", None) if gen is not None else None
    if not at or at[0] is None or time.monotonic() - at[1] > _INFLIGHT_S:
        return False
    seen = getattr(sup, "loading_since", None)
    return bool(seen(*at)) if callable(seen) else False


def _merchant(gate: Any) -> Optional[str]:
    if isinstance(gate.approved, tuple):
        return gate.approved[4]
    merchant = getattr(gate.gen, "merchant", None) if gate.gen is not None else None
    return merchant[2] if merchant else None


def _shop_page(sup: Any, gate: Any, url: Any) -> bool:
    """A document on the merchant's site: never another site (a bank's page) or a provider's or card origin."""
    from tools.browser_payment_surface_after_pay import _provider_site, cross_site

    merchant = _merchant(gate)
    return bool(merchant) and isinstance(url, str) and url.startswith(("https://", "http://")) \
        and not cross_site(url, merchant) and not _provider_site(sup, url)


async def _evidence(sup: Any, gate: Any, sid: str, row: tuple, checkout: bool) -> Optional[str]:
    from tools.browser_payment_order_evidence import order_evidence
    from tools.browser_payment_surface import pay_press

    if not _shop_page(sup, gate, row[2]):
        return None
    evidence, _reason = await order_evidence(sup, sid, row, checkout, pay_press(sup), need_wording=True)
    return evidence


async def _scan_top(sup: Any, gate: Any, page_sid: str) -> tuple:
    """``(found or None, top)``: the payment tab's top document ``(frame id, loader, url)`` and its evidence."""
    from tools import browser_payment_done as done
    from tools.browser_payment_surface import _top_frame, pay_press

    top = await _top_frame(sup, page_sid)
    evidence = await _evidence(sup, gate, page_sid, top, done.checkout_like_now(sup, top, pay_press(sup)))
    found = {"where": "top", "frame": top[0], "loader": top[1], "evidence": evidence} if evidence else None
    return found, top[:2]


async def page_tabs(sup: Any) -> Optional[frozenset]:
    """The page targets before a Pay-like press (a tab it opens is told apart by this); None: unread."""
    try:
        infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
        return frozenset(t.get("targetId") for t in infos if isinstance(t, dict) and t.get("type") == "page")
    except Exception as exc:  # health: allow BLE001 -- unread: no new tab is looked at after this press
        logger.info("payment_click: tabs before the press not read (%s)", type(exc).__name__)
        return None


async def _scan_new_tabs(sup: Any, gate: Any, before: frozenset) -> Optional[dict]:
    """A page target opened by the payment tab since ``before`` that shows order evidence (a new document)."""
    from tools import browser_payment_done as done
    from tools.browser_payment_surface import _top_frame

    opener = done.payment_tabs(sup)
    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    new = [t["targetId"] for t in infos if isinstance(t, dict) and t.get("type") == "page"
           and t.get("targetId") not in before and t.get("openerId") in opener]
    for tab in new[:_MAX_NEW_TABS]:
        sid, temporary = await done._tab_session(sup, tab)
        try:
            row = await _top_frame(sup, sid)
            evidence = await _evidence(sup, gate, sid, row, False)
        finally:
            if temporary:
                await sup._release_payment_sessions({"tab": sid})
        if evidence:
            return {"where": "new_tab", "tab": tab, "evidence": evidence}
    return None


def _spend(sup: Any, gate: Any, found: dict, caption: str) -> bool:
    """Book the detection on this approval's lock record (still the current one): the approval is spent."""
    from tools import browser_payment_surface as surface

    with surface._LOCK:
        gen = gate.gen
        if gen is None or surface._GENS.get(sup) is not gen:
            return False
        if not gen.paid:
            gen.paid, gen.paid_by = True, {**found, "caption": caption}
    logger.info("payment_click: payment detected (%s, %s); the approval is spent", found["where"], found["evidence"])
    return True


async def after_press(sup: Any, gate: Any, page_sid: str, before: Optional[frozenset], caption: str,
                      until: float) -> bool:
    """After a Pay-like press's effect read: order evidence on the top document (or a tab the press opened) spends
    the approval. Bounded by ``until`` (monotonic; the press already landed, so the answer must not time out);
    unread or no evidence: False."""
    async def look() -> Optional[dict]:
        found, _top = await _scan_top(sup, gate, page_sid)
        if found is None and before is not None:
            found = await _scan_new_tabs(sup, gate, before)
        return found

    try:
        found = await asyncio.wait_for(look(), timeout=max(0.1, until - time.monotonic()))
    except Exception as exc:  # health: allow BLE001 -- unread: payment_detected false; payment_done reads again
        logger.info("payment_click: payment detection not read (%s)", type(exc).__name__)
        return False
    return bool(found) and _spend(sup, gate, found, caption)


async def before_press(sup: Any, gate: Any, page_sid: str, deadline: float) -> tuple:
    """``(refusal or None, top scanned or None)`` before a Pay-like press that follows another one in this approval:
    the last one's navigation still loading (``payment_in_flight``), or order evidence shown now (spent:
    ``payment_already_made``). Unread: the press goes on (its total was read)."""
    gen = gate.gen
    if gen is None or gen.pay_at is None:
        return None, None
    if inflight(sup, gen):
        return {"success": False, "error_type": INFLIGHT, "error": _INFLIGHT_TEXT}, None
    try:
        found, top = await asyncio.wait_for(_scan_top(sup, gate, page_sid),
                                            timeout=max(0.1, min(_BEFORE_SCAN_S, deadline - time.monotonic())))
    except Exception as exc:  # health: allow BLE001 -- unread: nothing found; the in-flight check still runs last
        logger.info("payment_click: payment detection before the press not read (%s)", type(exc).__name__)
        return None, None
    if found and _spend(sup, gate, found, gen.pay_caption):
        return spent_refusal(gen), None
    return None, top


async def last_check(sup: Any, gate: Any, page_sid: str, scanned: Optional[tuple]) -> Optional[dict]:
    """The last await before a Pay-like press is booked (``_press_now``): still loading, spent meanwhile, or the
    top document is no longer the one ``before_press`` looked at (``payment_control_changed``)."""
    from tools.browser_payment_surface import _changed, _top_frame

    if inflight(sup, gate.gen):
        return {"success": False, "error_type": INFLIGHT, "error": _INFLIGHT_TEXT}
    if scanned is not None and tuple((await _top_frame(sup, page_sid))[:2]) != tuple(scanned):
        return _changed()
    return spent_refusal(gate.gen)
