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

Evidence counts only on a shop page: a document positively on the merchant's site (the same known site, or the exact
merchant origin; not another or an unknown site, a bank's 3-D Secure page, a provider's or card origin). Right before
each Pay-like press the same value-free scan runs and its booleans are kept with the press (build G2). On the very
document the press was made on, only a strong class (a thank-you-for-your-order sentence, an order number) that was
absent before the press counts, with no enabled purchase button and the pressed control gone; any other shop document
(a new loader, a new tab) needs order wording, and a checkout-like one no enabled purchase button. Failure wording
and the pressed Pay control still shown are never evidence. The document scanned is read again after the scan (frame,
loader, origin, shop page) and the approval is spent only when it did not change. Only fixed codes and booleans
leave the page.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_surface")

_INFLIGHT_S = 45.0  # the longest a Pay-like press's navigation holds the next Pay-like press back (build G2: 45 s)
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
    if by.get("returned"):  # D-0878: found by the order check after the owner returned the tab
        return {"success": False, "error_type": SPENT,
                "error": ("An order was detected on the payment tab when it came back to Hermes (the owner, Danil, "
                          f"had taken it over; {why}{where}), so nothing was pressed and no approval is open. Tell "
                          "him the order looks placed, and call payment_done: it reads the confirmation and ends "
                          "the lock. Never press Pay or Buy here again; a new purchase needs a new approval from "
                          "him.")}
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
    """A document positively on the merchant's site (build G2): both sites known and equal, else the exact merchant
    origin. An unknown site (an IP address, localhost, http, a single-label host) is never the shop unless it is the
    merchant's own origin; never a provider's or card origin."""
    from tools.browser_payment_surface_after_pay import _origin_of, _provider_site, _site

    merchant = _merchant(gate)
    if not merchant or not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return False
    a, b = _site(url), _site(merchant)
    same = a == b if a is not None and b is not None else bool(_origin_of(url)) and _origin_of(url) == _origin_of(merchant)
    return same and not _provider_site(sup, url)


async def _still_the_shop(sup: Any, gate: Any, sid: str, row: tuple) -> bool:
    """The document scanned (``row``: frame id, loader, url) is still the one on ``sid``'s top, with the same origin,
    and still a shop page (build G2: it could have been replaced, or redirected to a bank, during the scan)."""
    from tools.browser_payment_surface import _top_frame
    from tools.browser_payment_surface_after_pay import _origin_of

    now = await _top_frame(sup, sid)
    return tuple(now[:2]) == tuple(row[:2]) and _origin_of(now[2]) == _origin_of(row[2]) and _shop_page(sup, gate, now[2])


async def _scan_top(sup: Any, gate: Any, page_sid: str, base: Optional[dict], judged: bool) -> tuple:
    """``(found or None, mine)``: the payment tab's top document read once. ``mine``: ``{"doc": (frame id, loader),
    "strong": bool or None}``, the booleans kept with a Pay-like press (None: not a shop page, or unread). ``found``
    (only when ``judged``): the evidence, judged against ``base`` (the earlier look at this document; None: none read,
    which a checkout-like document cannot be judged without)."""
    from tools import browser_payment_done as done
    from tools.browser_payment_order_evidence import judge, scan_document
    from tools.browser_payment_surface import _top_frame, pay_press

    top = await _top_frame(sup, page_sid)
    mine = {"doc": tuple(top[:2]), "strong": None}
    if not _shop_page(sup, gate, top[2]):
        return None, mine
    scan = await scan_document(sup, page_sid, top)
    if scan is None:
        return None, mine
    mine["strong"] = scan["strong"]
    checkout = done.checkout_like_now(sup, top, pay_press(sup))
    if not judged or (checkout and base is None):
        return None, mine
    evidence, _reason, strong = await judge(sup, page_sid, top, scan, checkout, pay_press(sup), True, base)
    if evidence is None or not await _still_the_shop(sup, gate, page_sid, top):
        return None, mine
    return {"where": "top", "frame": top[0], "loader": top[1], "evidence": evidence, "strong": strong}, mine


async def page_tabs(sup: Any) -> Optional[frozenset]:
    """The page targets before a Pay-like press (a tab it opens is told apart by this); None: unread."""
    try:
        infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
        return frozenset(t.get("targetId") for t in infos if isinstance(t, dict) and t.get("type") == "page")
    except Exception as exc:  # health: allow BLE001 -- unread: no new tab is looked at after this press
        logger.info("payment_click: tabs before the press not read (%s)", type(exc).__name__)
        return None


async def _scan_tab(sup: Any, gate: Any, sid: str, strict: bool = False) -> Optional[dict]:
    """A tab's order evidence, or None. ``strict`` (D-0878, the order check after a return): a document not read in
    full, or replaced during the read, raises instead."""
    from tools.browser_payment_order_evidence import judge, scan_document
    from tools.browser_payment_surface import SurfaceRefused, _top_frame

    row = await _top_frame(sup, sid)
    if not _shop_page(sup, gate, row[2]):
        return None
    scan = await scan_document(sup, sid, row)
    if scan is None and strict:
        raise SurfaceRefused("order wording not read")
    if scan is None:
        return None
    evidence, _reason, strong = await judge(sup, sid, row, scan, False, None, True)
    if evidence is None:
        return None
    if not await _still_the_shop(sup, gate, sid, row):
        if strict:
            raise SurfaceRefused("the tab changed during the check")
        return None
    return {"evidence": evidence, "strong": strong}


async def _scan_new_tabs(sup: Any, gate: Any, before: frozenset, only: Optional[frozenset] = None,
                         record: Any = None) -> Optional[dict]:
    """A page target opened by the payment tab since ``before`` that shows order evidence (a new document).
    ``only`` (G3): look at these targets only (the ones a press opened, re-read before the next press); ``seen``: filled
    ``record``: the lock record whose ``pay_tabs`` gets every target the press opened (whatever it shows now),
    added before any tab is scanned."""
    from tools import browser_payment_done as done

    opener = done.payment_tabs(sup)
    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    opened = [t for t in infos if isinstance(t, dict) and t.get("type") == "page"
              and t.get("targetId") not in before and t.get("openerId") in opener]
    if record is not None:  # G3 (Opus RG5/RG6): every pass records, so a tab opened late is never lost
        record.pay_tabs = frozenset(record.pay_tabs or ()) | frozenset(t["targetId"] for t in opened)
    opened = [t for t in opened if only is None or t.get("targetId") in only]
    # G3 (Opus RG4 L4): another site's tab never uses the cap; the re-read of recorded tabs has no cap (RG6 L1: its
    # own time bound limits it)
    new = [t["targetId"] for t in opened if _shop_page(sup, gate, str(t.get("url") or ""))]
    for tab in (new if only is not None else new[:_MAX_NEW_TABS]):
        try:  # G3 (Opus RG3 M2): one unreadable tab does not stop the scan of the others
            sid, temporary = await done._tab_session(sup, tab)
            try:
                found = await _scan_tab(sup, gate, sid)
            finally:
                if temporary:
                    await sup._release_payment_sessions({"tab": sid})
        except Exception as exc:  # health: allow BLE001 -- this tab unread: the next one is still looked at
            logger.info("payment_click: a new tab not read (%s)", type(exc).__name__)
            continue
        if found:
            return {"where": "new_tab", "tab": tab, **found}
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
                      until: float, base: Optional[dict] = None) -> bool:
    """After a Pay-like press's effect read: order evidence on the top document (or a tab the press opened) spends
    the approval. ``base``: the booleans read right before this press (``before_press``). Bounded by ``until``
    (monotonic; the press already landed, so the answer must not time out); unread or no evidence: False."""
    gen = gate.gen

    async def look() -> Optional[dict]:
        if before is not None and gen is not None:  # G3 (Opus RG3-RG6): first record which tabs this press opened (all
            try:  # Pay presses of the approval add up), so a slow scan never loses them; a failed record never skips
                await asyncio.wait_for(_scan_new_tabs(sup, gate, before, only=frozenset(), record=gen), 2.0)
            except Exception as exc:  # health: allow BLE001 -- unrecorded: the scan below records again
                logger.info("payment_click: tabs after the press not listed (%s)", type(exc).__name__)
        found, _mine = await _scan_top(sup, gate, page_sid, base, True)
        if found is None and before is not None:
            found = await _scan_new_tabs(sup, gate, before, record=gen)
        return found

    try:
        found = await asyncio.wait_for(look(), timeout=max(0.1, until - time.monotonic()))
    except Exception as exc:  # health: allow BLE001 -- unread: payment_detected false; payment_done reads again
        logger.info("payment_click: payment detection not read (%s)", type(exc).__name__)
        return False
    return bool(found) and _spend(sup, gate, found, caption)


async def before_press(sup: Any, gate: Any, page_sid: str, deadline: float) -> tuple:
    """``(refusal or None, kept or None)`` right before a Pay-like press. ``kept``: the booleans of the top document
    now (``_scan_top``'s ``mine``), kept with the press: the document after it is judged against them. For a press
    that follows another one in this approval: the last one's navigation still loading (``payment_in_flight``), or
    order evidence shown now (spent: ``payment_already_made``), judged against the last press's booleans. Unread:
    the press goes on (its total was read)."""
    gen = gate.gen
    later = gen is not None and gen.pay_at is not None
    if later and inflight(sup, gen):
        return {"success": False, "error_type": INFLIGHT, "error": _INFLIGHT_TEXT}, None
    def budget() -> float:
        return max(0.1, min(_BEFORE_SCAN_S, deadline - time.monotonic()))

    if gen is not None and gen.returned:  # D-0878: the owner had the tab: one order check, read in full, whose look
        from tools.browser_payment_return import check  # at the top document is also this press's baseline

        return await check(sup, gate, page_sid, budget())
    found = mine = None
    try:
        found, mine = await asyncio.wait_for(
            _scan_top(sup, gate, page_sid, gen.pay_base if later else None, later), timeout=budget())
    except Exception as exc:  # health: allow BLE001 -- unread: nothing found; the tab re-read still runs
        logger.info("payment_click: payment detection before the press not read (%s)", type(exc).__name__)
    if found is None and later and gen.pay_tabs:  # G3: a confirmation tab a Pay press opened, loaded late; its own
        try:  # bound, so a slow tab never drops ``mine`` (Opus RG4 M2, RG5)
            found = await asyncio.wait_for(_scan_new_tabs(sup, gate, frozenset(), only=gen.pay_tabs),
                                           timeout=budget())
        except Exception as exc:  # health: allow BLE001 -- unread tabs: no evidence; the top document's look stays
            logger.info("payment_click: new tabs before the press not read (%s)", type(exc).__name__)
    if found and _spend(sup, gate, found, gen.pay_caption):
        return spent_refusal(gen), None
    return None, mine


async def last_check(sup: Any, gate: Any, page_sid: str, kept: Optional[dict]) -> Optional[dict]:
    """The last await before a Pay-like press is booked (``_press_now``): still loading, spent meanwhile, or the
    top document is no longer the one ``before_press`` looked at (``payment_control_changed``)."""
    from tools.browser_payment_surface import _changed, _top_frame

    if inflight(sup, gate.gen):
        return {"success": False, "error_type": INFLIGHT, "error": _INFLIGHT_TEXT}
    if kept is not None and tuple((await _top_frame(sup, page_sid))[:2]) != tuple(kept["doc"]):
        return _changed()
    return spent_refusal(gate.gen)
