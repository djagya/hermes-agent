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
- **Order check.** While a record is ``returned`` (until its next Pay-like press is booked), every Pay-like press
  and every payment_controls listing first runs the order detection on the payment tab (:func:`check`: the
  detection's own scan and judge). An order found spends the record (``payment_already_made``: the owner paid
  himself). A check that was not read in full (any CDP call of it failed or timed out, or the page changed under
  it) presses nothing (``payment_return_unchecked``).
- **Fresh approval.** The record comes back without its approved total, so a press needs what a lock without one
  needs: a new card fill (a new approval card; its record stays ``returned``) or the owner's go in chat.

The same holds when the supervisor was replaced for another reason while the lock held (the texts say so).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

from tools import browser_payment_detect as detect
from tools import browser_payment_surface as surface

logger = logging.getLogger("tools.browser_payment_surface")

# control-v1 additions (monolith services/browser): an older control answers both ``cdp_method_blocked``.
TICKET_METHOD = "Hermes.paymentSurfaceTicket"
ADOPT_METHOD = "Hermes.adoptPaymentSurface"
_TICKET_RE = re.compile(r"[0-9a-f]{64}")
_CALL_TIMEOUT_S = 4.0  # adoption, attach and ticket together stay inside the supervisor's start budget
_MAX_PARKED = 64
# task id -> (lock record, its payments): left by a supervisor that ended, until the task's next one dials.
# Read and written under ``surface._LOCK``.
_PARKED: dict = {}

RETURN_UNCHECKED = "payment_return_unchecked"
RETURNED = ("This payment came back to Hermes on a new browser connection (the owner, Danil, took the tab over and "
            "returned it, or the connection was replaced)")
_UNCHECKED_TEXT = (
    f"{RETURNED}, and Hermes could not check whether an order already went through (the page did not answer in full "
    "or in time, or it changed during the check), so nothing was pressed. Do not press Pay or Buy. Call "
    "payment_controls once more; if payment_detected is still null, tell Danil that the check failed and ask him to "
    "look at the tab, the shop's e-mail and his bank.")
_DETECTED_NEXT = (
    f"{RETURNED}, and the page shows an order was placed (payment_detected): tell him, and call payment_done now. "
    "Never press Pay or Buy here; a new purchase needs a new approval from him.")
_CLEAR_NEXT = (
    f"{RETURNED}; no placed order shows on it. No approval from before counts: before any Pay press, ask him for a "
    "fresh approval of this purchase (fill the card again with the current cart, or, when the page takes no card "
    "any more, his explicit go in chat and owner_go true).")


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
        if gen is None:
            return True
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


async def _look(sup: Any, gate: Any, page_sid: str) -> Optional[dict]:
    """The order detection on the returned payment tab: its top document when that is a shop page (as in the
    detection, no other page is evidence), then the tabs an earlier Pay-like press opened. The evidence, or None;
    raises when a read was not answered. With no look kept from a Pay-like press of this process, nothing is known
    about where the owner left the tab, so only the strong wording counts (a thank-you sentence, an order number):
    a cart's or checkout's own service text is not an order. That wording was not read before, so the evidence is
    not ``strong`` for payment_done (its card-frame rule stays)."""
    from tools import browser_payment_done as done
    from tools.browser_payment_order_evidence import judge, scan_document

    gen = gate.gen
    top = await surface._top_frame(sup, page_sid)
    if detect._shop_page(sup, gate, top[2]):
        scan = await scan_document(sup, page_sid, top)
        if scan is None:
            raise surface.SurfaceRefused("order wording not read")
        pay = surface.pay_press(sup)
        base = gen.pay_base or {"doc": tuple(top[:2]), "strong": False}
        evidence, _reason, strong = await judge(sup, page_sid, top, scan, done.checkout_like_now(sup, top, pay), pay,
                                                True, base)
        if evidence is not None:
            if not await detect._still_the_shop(sup, gate, page_sid, top):
                raise surface.SurfaceRefused("the page changed during the check")
            return {"where": "top", "frame": top[0], "loader": top[1], "evidence": evidence,
                    "strong": strong and gen.pay_base is not None}
    if gen.pay_tabs:
        return await detect._scan_new_tabs(sup, gate, frozenset(), only=gen.pay_tabs)
    return None


async def check(sup: Any, gate: Any, page_sid: str, budget: float) -> Optional[dict]:
    """The order check of a returned record (``gate.gen``), within ``budget`` seconds. None: it was read in full and
    no order shows. Else the refusal for a press: ``payment_already_made`` (found now or before: the record is
    spent) or ``payment_return_unchecked`` (any CDP call of the check failed or timed out, or it could not judge)."""
    from tools.browser_supervisor import CDP_FAILURES

    gen = gate.gen
    if gen.paid:
        return detect.spent_refusal(gen)
    failed: list = []
    token = CDP_FAILURES.set(failed)
    found = None
    try:
        found = await asyncio.wait_for(_look(sup, gate, page_sid), timeout=max(0.1, budget))
    except Exception as exc:  # health: allow BLE001 -- unread: nothing is pressed (the type only; CDP text quotes pages)
        failed.append(type(exc).__name__)
    finally:
        CDP_FAILURES.reset(token)
    if failed or (found and not detect._spend(sup, gate, {**found, "returned": True}, "")):
        logger.info("payment return: order check not read (%s)", ", ".join(failed[:4]) or "record replaced")
        return surface._refusal(RETURN_UNCHECKED, _UNCHECKED_TEXT)
    return detect.spent_refusal(gen) if found else None


async def report(sup: Any, page_sid: str) -> dict:
    """What payment_controls adds for a returned record (nothing otherwise): ``returned``, ``payment_detected``
    (true, false, or null when the check was not read) and what to do next."""
    with surface._LOCK:
        gen = surface._GENS.get(sup) if sup in surface._HELD else None
        if gen is None or not gen.returned:
            return {}
        gate = surface._Gate(gen.approved, gen.admitted, False, surface._EPOCHS.get(sup, 0), gen)
    refused = await check(sup, gate, page_sid, detect._BEFORE_SCAN_S)
    detected = None if refused and refused["error_type"] == RETURN_UNCHECKED else bool(refused)
    out = {"returned": True, "payment_detected": detected}
    if detected is not False or gate.approved is None:  # a fresh approval is bound: nothing more to ask for
        out["next"] = {True: _DETECTED_NEXT, False: _CLEAR_NEXT, None: _UNCHECKED_TEXT}[detected]
    return out
