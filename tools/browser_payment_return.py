"""dlz (D-0878 a, owner 2026-11-24): Sera takes a card payment back after the owner took the tab and pressed
Return to Sera.

A take ends the lease the payment's supervisor dialled with: its redial is refused (403), its thread ends, and the
payment's lock record (weak-keyed by that supervisor) used to end with it; the task's next supervisor then met
"another connection's lock". Now the record has one owner at every moment and moves:

- **Ticket.** After each confirmed claim the supervisor asks control for the lock's adoption ticket
  (``Hermes.paymentSurfaceTicket``, answered on the lock's native connection only) and keeps it with the lock
  record, in memory only. It proves to control that a later connection belongs to the process that made the claim:
  model code can read a lease token, never this.
- **Park.** A supervisor that is done (stopped, or its connection is gone for good) leaves its lock record to its
  task (:func:`park`) before it leaves the registry, so a replacement always finds it.
- **Take back.** The task's next supervisor takes that record on its first dial (:func:`take_back`) and sends
  ``Hermes.adoptPaymentSurface {ticket}``. Control confirmed (this socket is now the lock's native connection) and
  the payment tab attached: the record is live on this socket, marked ``returned``, with no approval and with
  Sera's presses in the lock still counted. Anything else (no ticket, an older control, a refusal, a timeout,
  another reply, a failed attach): the record is lost for good (it stays lost on every later supervisor too), every
  payment action answers ``payment_native_lost`` and nothing is pressed.
- **Fresh approval** (D-0922 a, D-0942 a, D-0961). The record comes back without its approved total and stays
  ``returned`` until the lock ends. owner_go does not count on it: only a new card fill lets Sera press Pay, and
  until its approval is bound she accepts no confirm dialog of the shop (:func:`awaits_approval`). Before that fill's
  approval card is built, the payment tab's top document is read once (:func:`observe`: the value-free wording scan,
  bounded); the card says what it saw ("Order wording seen on the checked page", none seen, or not read), how often
  Sera already pressed Pay in this lock, and that Approve confirms the owner has not already paid. The read only
  advises: it never refuses a press and never spends the record. The owner's tap is the answer; after it Sera presses
  what the checkout needs, as on a lock that was never returned (D-0961: no step is left to the owner). A page past
  the card step (the fill finds no card fields; :func:`press_page`: the record's merchant shows the stated total and
  a Pay-like control) gets the same card in its press-only form; the tap binds the total as a fill's would.
- **Any other page**: payment_done on an order confirmation, else the base way out (``RETURNED_REFILL``). That the
  payment came back and Sera's Pay presses belong to the task (``_RETURNED``): that fill's card still says both.

The same holds when the supervisor was replaced for another reason while the lock held (the texts say so).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from tools import browser_payment_surface as surface

logger = logging.getLogger("tools.browser_payment_surface")

# control-v1 additions (monolith services/browser): an older control answers both ``cdp_method_blocked``.
TICKET_METHOD = "Hermes.paymentSurfaceTicket"
ADOPT_METHOD = "Hermes.adoptPaymentSurface"
_TICKET_RE = re.compile(r"[0-9a-f]{64}")
# each of adoption, attach and ticket (after the dial): on a start, a slow control can pass start()'s 15 s budget;
# start() then fails and the record is parked again (lost if control had already spent the ticket): nothing is pressed
_CALL_TIMEOUT_S = 4.0
_MAX_PARKED = 64
# task id -> (lock record, its payments): left by a supervisor that ended, until the task's next one dials.
# Read and written under ``surface._LOCK``.
_PARKED: dict = {}
# task id -> the task's latest returned lock record (its lock may have ended: payment_close), until payment_done
# reads the order confirmation: the task's next lock starts from it (:func:`resume`). Under ``surface._LOCK``.
_RETURNED: dict = {}

_OBSERVE_TIMEOUT_S = 6.0  # the one read of the page before a fresh approval card (unread: the card says so)
RETURNED_NEXT = (
    "This payment came back to Hermes on a new browser connection (the owner, Danil, took the tab over and returned "
    "it, or the connection was replaced). No approval from before counts and owner_go does not count: before any Pay "
    "press or confirm, fill the card again with the current cart (on a page past the card step the same call asks him "
    "only to approve the Pay press). Its approval card tells him what the page shows now and asks him to confirm he "
    "has not already paid; his Approve covers the presses this checkout needs. If the page shows an order "
    "confirmation, call payment_done instead; a fill this page cannot take says how to go on.")
RETURNED_REFILL = (  # the one way out of a returned lock on a page that takes no fill and shows no order
    "call browser_vault payment_close (it closes the payment tab and ends the lock), open the checkout again, then "
    "fill once more: that approval card still says the payment came back and how often you pressed Pay. If this "
    "happens a second time, stop and tell the user.")
RETURNED_CLOSED = ("The payment tab was closed without a confirmation. Open the checkout again and fill: its approval "
                   "card tells the owner that this payment came back and how often you pressed Pay, and he decides.")
LOCK_MOVING = (
    "Nothing was read, written or pressed: the owner has this payment's tab, or it is moving to a new browser "
    "connection of this task (he returned it, or the connection was replaced). Retry this call: once the tab is back, "
    "the next payment call takes the payment back.")
PRESS_APPROVED = ("The owner approved pressing Pay on this page as it is (nothing was filled: the card is already "
                  "entered). Call payment_controls and press what the checkout needs.")


async def _ticket(sup: Any) -> Optional[str]:
    """Control's adoption ticket for the lock this socket is native to, or None (an older control, no answer,
    another shape): a take of the tab then ends in ``payment_native_lost``, as before."""
    try:
        reply = await sup._cdp(TICKET_METHOD, {}, timeout=_CALL_TIMEOUT_S)
        result = reply.get("result") if isinstance(reply, dict) else None
        if isinstance(result, dict) and set(result) == {"ticket"} and isinstance(result["ticket"], str) \
                and _TICKET_RE.fullmatch(result["ticket"]):
            return result["ticket"]
    except Exception as exc:  # health: allow BLE001 -- no ticket: nothing adopts this lock later (fail closed)
        logger.info("payment lock has no adoption ticket (%s)", type(exc).__name__)
    return None


def keep_ticket(supervisor: Any, loop: Any, ws: Any) -> None:
    """After a confirmed claim on ``ws`` (tool thread): the lock's ticket, kept with the record the claim started."""
    try:
        ticket = surface._wait(_ticket(supervisor), loop, timeout=_CALL_TIMEOUT_S + 2)
    except Exception as exc:  # health: allow BLE001 -- the loop did not answer: no ticket
        logger.info("payment lock has no adoption ticket (%s)", type(exc).__name__)
        ticket = None
    with surface._LOCK:
        gen = surface._GENS.get(supervisor) if supervisor in surface._HELD else None
        if gen is not None and gen.ws is ws:
            gen.ticket = ticket


def park(supervisor: Any) -> None:
    """``supervisor``'s thread ended. A lock record it holds (a confirmed claim not proven released) and its payments
    leave it and wait for the task's next supervisor; an older parked record of the task is replaced."""
    from tools import browser_payment_done as done

    with surface._LOCK:
        gen = surface._GENS.get(supervisor) if supervisor in surface._HELD else None
        if gen is None or gen.owned is None:
            return
        surface._GENS.pop(supervisor, None)
        surface._HELD.discard(supervisor)
    with done._LOCK:
        payments = done._PAYMENTS.pop(supervisor, None) or {}
    with surface._LOCK:
        _PARKED.pop(supervisor.task_id, None)
        _PARKED[supervisor.task_id] = (gen, payments)
        while len(_PARKED) > _MAX_PARKED:
            _PARKED.pop(next(iter(_PARKED)))
    logger.info("payment lock record %d left to its task (its supervisor ended)", gen.serial)


def stranded(sup: Any) -> bool:
    """A lock record of ``sup``'s task was parked after ``sup`` dialled (it lost a start race to the supervisor that
    took the record back, or a stop parked it late): ``sup`` holds no record and serves the lock as another
    connection's. ``get_or_start`` replaces it; the replacement takes the record back on its first dial."""
    with surface._LOCK:
        if getattr(sup, "task_id", None) not in _PARKED or sup in surface._HELD:
            return False
    with sup._state_lock:
        return not sup._active


def came_back(task_id: str) -> str:
    """What a stop that wrote nothing says: ``RETURNED_NEXT`` when a lock record of ``task_id`` came back (held, not
    lost), ``LOCK_MOVING`` while one waits to be taken back (the owner may still have the tab), else ``""``."""
    with surface._LOCK:
        parked = _PARKED.get(task_id)
        if parked is not None:
            return "" if parked[0].native_lost else LOCK_MOVING
        held = any(getattr(s, "task_id", None) == task_id and g.returned and not g.native_lost
                   for s, g in list(surface._GENS.items()) if s in surface._HELD)
        return "Nothing was written or pressed. " + RETURNED_NEXT if held else ""


def _adopted(result: Any, tab: str) -> Optional[frozenset]:
    """Control confirmed the adoption: exactly ``{adopted: true, locked: true, tabs}`` with the payment tab among the
    lock's owning tabs. Returns them, else None."""
    if not (isinstance(result, dict) and set(result) == {"adopted", "locked", "tabs"} and result["adopted"] is True
            and result["locked"] is True and isinstance(result["tabs"], list)
            and len(result["tabs"]) <= surface._MAX_STATUS_TABS and all(surface._valid_id(t) for t in result["tabs"])
            and tab in result["tabs"]):
        return None
    return frozenset(result["tabs"])


async def take_back(sup: Any) -> bool:
    """On ``sup``'s fresh socket, before anything is attached. False: the task's parked lock record moved to ``sup``
    and could not be adopted, so it is lost (status only; ``payment_native_lost``). True: nothing was parked, or
    control made this socket the lock's native connection, the payment tab is attached (``sup._page_session_id``)
    and control gave the next ticket (it spent the old one): the record is ``returned``, without its approval; its
    presses stay counted (all of them were booked in it). Raises only when cancelled (the record stays with ``sup``,
    on another socket's claim: lost unless a later supervisor's adoption is confirmed)."""
    from tools import browser_payment_done as done
    from tools.browser_supervisor import PAGE_ATTACH_TIMEOUT_S

    with surface._LOCK:
        gen, payments = _PARKED.pop(sup.task_id, (None, None))
    if gen is None or not surface._managed():  # a browser without browser-control holds no lock to take back
        return True
    with surface._LOCK:
        surface._HELD.add(sup)  # from here ``sup`` is the record's one owner; its claim socket is not this one
        surface._GENS[sup] = gen
    with done._LOCK:
        done._PAYMENTS[sup] = payments
    ws = sup._ws
    try:
        if gen.native_lost or not gen.ticket or len(payments) != 1:
            raise surface.SurfaceRefused("nothing to adopt with")
        tab = next(iter(payments))
        reply = await sup._cdp(ADOPT_METHOD, {"ticket": gen.ticket}, timeout=_CALL_TIMEOUT_S)
        owned = _adopted(reply.get("result") if isinstance(reply, dict) else None, tab)
        if owned is None:
            raise surface.SurfaceRefused("adoption not confirmed")
        sid = await sup._attach_page(tab, timeout=PAGE_ATTACH_TIMEOUT_S,  # locked: no dialog bridge is installed
                                     deadline=time.monotonic() + PAGE_ATTACH_TIMEOUT_S)
        ticket = await _ticket(sup)
        if ticket is None:
            raise surface.SurfaceRefused("no ticket after the adoption")
    except Exception as exc:  # health: allow BLE001 -- control's state is unknown or says no: lost, never retried
        with surface._LOCK:
            gen.native_lost = True
        logger.warning("payment lock record %d not taken back (%s)", gen.serial, type(exc).__name__)
        return False
    with surface._LOCK:
        gen.ws, gen.owned, gen.returned, gen.ticket = ws, owned, True, ticket
        gen.approved, gen.admitted, gen.pay_at = None, False, None
        _RETURNED[sup.task_id] = gen
        while len(_RETURNED) > _MAX_PARKED:
            _RETURNED.pop(next(iter(_RETURNED)))
    sup._page_session_id = sid
    logger.warning("payment lock record %d taken back after a return (owning ids %d); its approval is void",
                   gen.serial, len(owned))
    return True


def _record(sup: Any) -> Any:
    """``sup``'s current lock record when it came back after a return, else None. Must hold ``surface._LOCK``."""
    gen = surface._GENS.get(sup) if sup in surface._HELD else None
    return gen if gen is not None and gen.returned else None


def resume(sup: Any, rec: Any) -> None:
    """Must hold ``surface._LOCK``. ``rec``: the record ``sup``'s confirmed claim just started. While the task's
    returned payment has not reached payment_done, ``rec`` is returned too and counts its Pay presses on (unknown stays
    unknown), also after that lock ended (payment_close, the checkout again). A detected order ends with its lock."""
    old = _RETURNED.get(getattr(sup, "task_id", None))
    if old is not None:
        if not rec.returned:  # not that lock's own re-fill, which ``_start_record`` carried
            rec.returned, rec.known, rec.pay_presses = True, rec.known and old.known, rec.pay_presses + old.pay_presses
        _RETURNED[sup.task_id] = rec


def settle(sup: Any) -> None:
    """payment_done read the order confirmation: the task's next lock starts as any other."""
    with surface._LOCK:
        _RETURNED.pop(getattr(sup, "task_id", None), None)


def awaits_approval(sup: Any) -> bool:
    """``sup``'s lock record came back after a return and no fresh approval is bound to it (a shop's confirm
    accepted now could place the order without one)."""
    with surface._LOCK:
        gen = _record(sup)
        return gen is not None and gen.approved is None


def report(sup: Any) -> dict:
    """What payment_controls adds for a returned record (nothing otherwise): ``returned``, and what to do next
    while no fresh approval is bound."""
    with surface._LOCK:
        gen = _record(sup)
        if gen is None:
            return {}
        return {"returned": True, **({"next": RETURNED_NEXT} if gen.approved is None else {})}


async def _wording(sup: Any) -> Optional[dict]:
    from tools.browser_payment_order_evidence import scan_document

    with sup._state_lock:
        sid = sup._page_session_id if sup._active else None
    return await scan_document(sup, sid, await surface._top_frame(sup, sid)) if sid else None


def observe(sup: Any) -> dict:
    """Before a card fill's approval card is built (tool thread): the card's facts of a return
    (``PaymentPromptFacts.returned`` and ``pay_presses``), ``{}`` when neither ``sup`` nor its task has a returned one.
    ``returned``: the one bounded, value-free read of the payment tab's top document (two booleans leave the page):
    ``"seen"`` (order wording), ``"not_seen"``, or ``"unread"`` (failed or timed out; not read again).
    ``pay_presses``: Sera's Pay-like presses in this lock (None: unknown). For the card only."""
    with surface._LOCK:
        gen = _record(sup) or _RETURNED.get(getattr(sup, "task_id", None))
        if gen is None:
            return {}
        presses = gen.pay_presses if gen.known else None
    try:
        scan = surface._wait(_wording(sup), sup._loop, timeout=_OBSERVE_TIMEOUT_S)
    except Exception as exc:  # health: allow BLE001 -- unread: the card says it could not check (the type only)
        logger.info("payment return: page not read for the approval card (%s)", type(exc).__name__)
        scan = None
    return {"returned": "unread" if scan is None else "seen" if scan["order"] else "not_seen", "pay_presses": presses}


async def _press_page(sup: Any, cents: int, currency: str, merchant: str) -> Optional[tuple]:
    page_sid, _tree, child_sessions, listed = await surface._list_all(sup)
    try:
        top = await surface._top_frame(sup, page_sid)
        pay = any(fid == top[0] and not c["disabled"] and surface._pay_press(c["role"], c["text"])
                  for fid, _sid, _arr, controls, _loader in listed for c in controls)
        good = pay and surface._origin_of(top[2]) == merchant and await surface.shows_total(sup, page_sid, top, cents,
                                                                                             currency)
        return tuple(top[:2]) if good else None
    finally:
        await surface._release(sup, child_sessions, listed)


def press_page(sup: Any, cents: int, currency: str) -> Optional[tuple]:
    """D-0961 (tool thread): the fill found no card fields. ``(record, top document, merchant)`` when ``sup``'s returned
    record admitted no press under its current approval (none yet, or its bound page loaded again before the first
    press) and the payment tab's top document is on the record's merchant, shows the total (the first press's own
    check) and an enabled Pay-like control; else None (also when the read fails)."""
    with surface._LOCK:
        gen = _record(sup)
        if gen is None or gen.admitted or gen.native_lost or not gen.merchant:
            return None
        merchant = gen.merchant[2]
    try:
        top = surface._wait(_press_page(sup, cents, currency, merchant), sup._loop, timeout=surface._ACTION_TIMEOUT_S)
    except Exception as exc:  # health: allow BLE001 -- unread: no press-only card (the fill's refusal stands)
        logger.info("payment return: page past the card step not read (%s)", type(exc).__name__)
        return None
    return (gen, top, merchant) if top else None


def bind_press(sup: Any, gen: Any, cents: int, currency: str, top: tuple, merchant: str) -> bool:
    """The owner's tap on the press-only card: the total and top document bound to ``gen`` as a fill's tap binds them
    (replacing an approval that admitted no press), while ``gen`` is still ``sup``'s returned record (else nothing: a
    second return) and no press was admitted since the card was offered."""
    with surface._LOCK:
        if _record(sup) is not gen or gen.admitted or gen.native_lost \
                or not surface._valid_checkout(top, merchant):
            return False
        gen.approved = (cents, currency, *top, merchant)
        return True
