"""dlz (D-0878 a, owner 2026-11-24): Sera takes a card payment back after the owner took the tab and pressed
Return to Sera.

A take ends the lease the payment's supervisor dialled with: its redial is refused (403), its thread ends, and the
payment's lock record (weak-keyed by that supervisor) used to end with it; the task's next supervisor then met
"another connection's lock". Now the record has one owner at every moment and moves:

- **Ticket.** After each confirmed claim the supervisor asks control for the lock's adoption ticket
  (``Hermes.paymentSurfaceTicket``, answered on the lock's native connection only) and keeps it with the lock
  record, in memory only. It proves to control that a later connection belongs to the process that made the claim:
  model code can read a lease token, never this.
- **Park.** A supervisor whose thread ends while it holds a lock record leaves the record to its task
  (:func:`park`).
- **Take back.** The task's next supervisor takes that record on its first dial (:func:`take_back`) and sends
  ``Hermes.adoptPaymentSurface {ticket}``. Control confirmed (this socket is now the lock's native connection) and
  the payment tab attached: the record is live on this socket, marked ``returned``, with no approval. Anything else
  (no ticket, an older control, a refusal, a timeout, another reply, a failed attach): the record is lost for good
  (it stays lost on every later supervisor too), every payment action answers ``payment_native_lost`` and nothing
  is pressed.
- **Fresh approval** (D-0922 a, D-0942 a). The record comes back without its approved total and stays ``returned``
  until the lock ends. owner_go does not count on it: only a new card fill lets Sera press Pay. Before that fill's
  approval card is built, the payment tab's top document is read once (:func:`observe`: the value-free wording scan,
  bounded); the card says what it saw ("Order wording seen on the checked page", none seen, or not read) and that
  Approve confirms the owner has not already paid. The read only advises: it never refuses a press and never spends
  the record. The owner's tap is the answer.
- **One Pay press.** An approval given on a returned record covers one Pay-like press of the checkout, booked at its
  dispatch (``surface._book_press``): a second one is refused (``payment_press_used``) whatever the first one's
  outcome was; only a bank's step after it (3-D Secure) still goes. On a page past the card step Sera asks the owner
  to press Pay himself, and she accepts no confirm dialog of the shop before her one press (:func:`awaits_press`).

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

_OBSERVE_TIMEOUT_S = 6.0  # the one read of the page before a fresh approval card (unread: the card says so)
RETURNED = ("This payment came back to Hermes on a new browser connection (the owner, Danil, took the tab over and "
            "returned it, or the connection was replaced)")
RETURNED_NEXT = (
    f"{RETURNED}. No approval from before counts and owner_go does not count: before any Pay press, fill the card "
    "again with the current cart. Its approval card tells him what the page shows now and asks him to confirm he "
    "has not already paid; his Approve covers one Pay press. When the page takes no card any more, ask him to press "
    "Pay himself in the tab. If he says he already paid, call payment_done.")
SEEN_NOTE = ("The payment tab showed order wording when it came back to Hermes, and the approval card told the owner "
             "(Danil) so. Say it in chat too: the page may already show a placed order; if he paid himself, call "
             "payment_done and press nothing.")
PRESS_USED = "payment_press_used"
PRESS_USED_TEXT = (
    f"{RETURNED}, and the one Pay press his fresh approval covered was already sent (whatever came of it), so "
    "nothing was pressed. Call payment_done to read where the payment tab is. If no order shows, tell him and ask: "
    "another Pay press needs a new approval card (fill the card again), or he presses Pay himself in the tab.")


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
    and control gave the next ticket (it spent the old one): the record is ``returned``, without its approval, and
    its presses are unknown. Raises only when cancelled (the record stays with ``sup``, on another socket's claim:
    lost unless a later supervisor's adoption is confirmed)."""
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
        gen.ws, gen.owned, gen.returned, gen.known, gen.ticket = ws, owned, True, False, ticket
        gen.approved, gen.admitted, gen.pay_at = None, False, None
    sup._page_session_id = sid
    logger.warning("payment lock record %d taken back after a return (owning ids %d); its approval is void",
                   gen.serial, len(owned))
    return True




def _record(sup: Any) -> Any:
    """``sup``'s current lock record when it came back after a return, else None. Must hold ``surface._LOCK``."""
    gen = surface._GENS.get(sup) if sup in surface._HELD else None
    return gen if gen is not None and gen.returned else None


def awaits_press(sup: Any) -> bool:
    """``sup``'s lock record came back after a return and no Pay-like press under a fresh approval is booked yet
    (a shop's confirm accepted now could place the order without one)."""
    with surface._LOCK:
        gen = _record(sup)
        return gen is not None and gen.pay_at is None


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


def observe(sup: Any) -> str:
    """Before a card fill's approval card is built (tool thread). ``""``: ``sup`` holds no returned record. Else
    the one bounded, value-free read of the payment tab's top document (two booleans leave the page): ``"seen"``
    (order wording), ``"not_seen"``, or ``"unread"`` (failed or timed out; not read again). For the card only."""
    with surface._LOCK:
        if _record(sup) is None:
            return ""
    try:
        scan = surface._wait(_wording(sup), sup._loop, timeout=_OBSERVE_TIMEOUT_S)
    except Exception as exc:  # health: allow BLE001 -- unread: the card says it could not check (the type only)
        logger.info("payment return: page not read for the approval card (%s)", type(exc).__name__)
        scan = None
    return "unread" if scan is None else "seen" if scan["order"] else "not_seen"
