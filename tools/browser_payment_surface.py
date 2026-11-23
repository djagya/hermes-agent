"""Payment slot lockdown, runtime side (lockdown design §1, contract ``cdp_methods`` in control-v1).

browser-control locks the slot when the runtime claims it: every non-native CDP connection to it is
closed, and while locked a non-native connection may only list and close tabs (``locked.commands``).
Control does not filter the runtime's own (native) supervisor socket, so this module is the lock for
everything the model can relay over that socket:

- :func:`claim_payment_surface` — the fill path's hook. Before any card value is resolved, the
  supervisor claims the payment tab (the page target holding the card documents; OOPIF card frames
  belong to it) with ``Hermes.claimPaymentSurface {tabs}``. A refused or unconfirmed claim refuses the
  fill (``payment_surface_unavailable``); nothing is resolved or written.
- :func:`slot_locked` / :func:`locked_refusal` — the one lock check for model-reachable calls on the
  native socket (``browser_cdp(frame_id=…)``, dialog answers and dialog text, the vault's non-card page
  operations). While locked only :data:`LOCKED_COMMANDS` without a session pass, which on this socket
  (every relayed call is session-scoped) means none. A call that was in flight when a claim started
  has its result withheld.
- :func:`payment_controls` / :func:`payment_click` — the native actions that let a checkout continue
  under the lock: list the buttons, links and checkboxes of the payment's card documents and of the
  payment tab's own top document (never inputs or selects; captions run through the supervisor-scoped
  payment text redactor before their refs are built), and make one trusted click on a listed control at
  its hit-tested centre.

The lock lifts only when control's status proves the slot unlocked (the payment tab destroyed, a
wipe or a human clear); a failed or malformed status keeps it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import weakref
from typing import Any, NamedTuple, Optional

from tools.browser_payment_surface_after_pay import (  # build C r4: site rules
    _DIALOG_SEL,
    _provider_site,
    cross_site,
)
from tools.browser_payment_press_kind import (  # build F/G1: which press is Pay-like
    CAPTION_WITHHELD,
    _pay_press,
    pay_node,
)

logger = logging.getLogger(__name__)

CLAIM_METHOD = "Hermes.claimPaymentSurface"
STATUS_METHOD = "Hermes.paymentSurfaceStatus"
# dlz (build C1): control names the tab-type holders of the latest native claim (additive; old control: blocked)
HOLDERS_METHOD = "Hermes.paymentSurfaceHolders"
UNAVAILABLE = "payment_surface_unavailable"
LOCKED = "payment_surface_locked"
# control-v1 ``cdp_methods.locked.commands`` (exact names); allowed only without a sessionId.
LOCKED_COMMANDS = frozenset({"Target.getTargets", "Target.getTargetInfo", "Target.setDiscoverTargets",
                             "Target.closeTarget", "Browser.getVersion"})
_MAX_CLAIM_TABS = 8
_MAX_STATUS_TABS = 64
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_CLAIM_TIMEOUT_S = 10.0
# payment_controls / payment_click: the caller waits _ACTION_TIMEOUT_S; the click's mousePressed is only
# dispatched before (start + _ACTION_TIMEOUT_S - _PRESS_MARGIN_S), so a press can never start after the
# caller gave up and reported that nothing was clicked.
_ACTION_TIMEOUT_S = 25.0
_PRESS_MARGIN_S = 5.0
_MAX_CONTROLS = 40
_MAX_TOP_CONTROLS = 150  # a merchant page's header and footer links come before its Pay button
_MAX_TEXT = 60
_REF_RE = re.compile(r"c\d{1,3}-[0-9a-f]{10}")


class SurfaceRefused(RuntimeError):
    """The payment surface could not be claimed or used; carries a model-safe reason."""


_LOCK = threading.Lock()
# Supervisors whose claim was sent and not proven released (fail closed between claim and status).
_HELD: weakref.WeakSet = weakref.WeakSet()
# Supervisors control last reported locked (read by loop-side checks that cannot wait for a status).
_KNOWN_LOCKED: weakref.WeakSet = weakref.WeakSet()
# build C4: supervisors control last reported locked with their socket not the lock's native connection.
_NOT_NATIVE: weakref.WeakSet = weakref.WeakSet()
# Claims on the wire per supervisor (a count, so no completion clears another claim's mark).
_CLAIMING: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# One claim, dialog answer or recovery call at a time per supervisor (claims are serialized).
_CLAIM_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# Per supervisor, bumped before each of its claims is sent: a relayed call whose reply straddles a claim on
# its own slot is withheld, and a status read that started before such a claim never clears the lock or
# counts as a release. Another task's claim (another supervisor) changes nothing here.
_EPOCHS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# A fenced native op (dialog answer, vault dispatch, recovery call) waits this long for the claim lock and holds
# it for about 29 s at worst (status read 7 s, one evaluate 11 s, its reference-chain retry 11 s; a timed-out
# call adds its drain, up to 5 s, only when the work ignores cancellation). A claim waits _CLAIM_WAIT_S for it,
# so a card fill is normally not refused because such an op was running; if the wait does run out, the fill is
# refused (payment_surface_unavailable), never sent unlocked.
_FENCE_WAIT_S = 15.0
_CLAIM_WAIT_S = 30.0


def _refusal(error_type: str, message: str) -> dict:
    return {"success": False, "error_type": error_type, "error": message}


_UNAVAILABLE_TEXT = (
    "Browser control could not lock this browser for the card payment, so no card was read or written. "
    "Nothing was submitted. Do not retry on another route; report it to the user."
)
LOCKED_TEXT = (
    "The browser is locked for a card payment: page reads, scripts, input, screenshots and attaching to tabs "
    "are refused until the payment tab is closed. Continue with browser_vault payment_controls / payment_click on "
    "the payment tab, and call payment_done after Pay: it hands the confirmation over and ends the lock. If the "
    "payment will not go ahead (a stopped fill, the user cancelled), call browser_vault payment_close: it closes "
    "the payment tab and ends the lock, without a confirmation."
)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _wait(coro, loop, *, timeout: float):
    """Run ``coro`` on the supervisor loop from a sync caller; on a timeout (or any failure to wait)
    the coroutine is cancelled and drained, so a late CDP reply can never let it act after the caller
    reported (or released a fence)."""
    from tools.browser_supervisor import run_bounded

    return run_bounded(coro, loop, timeout=timeout, unavailable=SurfaceRefused("supervisor loop unavailable"))


def _control_refused(exc: BaseException) -> bool:
    """Control answered the claim with its definitive refusal (the CDP error reply): it changed nothing.
    Anything else (timeout, cancel, transport, malformed reply) leaves control's state unknown."""
    return isinstance(exc, RuntimeError) and str(exc).startswith("CDP error on id=") and UNAVAILABLE in str(exc)


def parse_claim_result(result: Any, tabs: list) -> bool:
    """Control confirmed the claim: exactly ``{claimed: true, locked: true, tabs}`` with every claimed
    tab among the slot's owning tabs. Anything else is unconfirmed."""
    return (isinstance(result, dict) and set(result) == {"claimed", "locked", "tabs"}
            and result["claimed"] is True and result["locked"] is True and isinstance(result["tabs"], list)
            and 0 < len(result["tabs"]) <= _MAX_STATUS_TABS and all(_valid_id(t) for t in result["tabs"])
            and set(tabs) <= set(result["tabs"]))


def parse_lock_status(result: Any) -> tuple[str, frozenset]:
    """``Hermes.paymentSurfaceStatus`` as ``("locked", owning tabs)``, ``("unlocked", ∅)`` or
    ``("unknown", ∅)``. The one reader of the lock state. Exactly ``{locked, native, tabs, unverified}``:
    three bools and the sorted owning tab ids; ``locked`` must equal ``tabs or unverified`` and ``native``
    implies ``locked``. Any other shape, type or combination is unknown, never unlocked."""
    return parse_lock_status_native(result)[:2]


def parse_lock_status_native(result: Any) -> tuple[str, frozenset, Optional[bool]]:
    """:func:`parse_lock_status` plus control's ``native`` (whether the asking connection is the lock's native
    one): a bool when the state is ``locked``, else None."""
    if not isinstance(result, dict) or set(result) != {"locked", "native", "tabs", "unverified"}:
        return "unknown", frozenset(), None
    locked, native, tabs, unverified = result["locked"], result["native"], result["tabs"], result["unverified"]
    if not (type(locked) is bool and type(native) is bool and type(unverified) is bool):
        return "unknown", frozenset(), None
    if not (isinstance(tabs, list) and len(tabs) <= _MAX_STATUS_TABS and all(_valid_id(t) for t in tabs)
            and len(set(tabs)) == len(tabs)):
        return "unknown", frozenset(), None
    if locked is not (bool(tabs) or unverified) or (native and not locked):
        return "unknown", frozenset(), None
    return ("locked", frozenset(tabs), native) if locked else ("unlocked", frozenset(), None)


def parse_holders(result: Any, tab: str, owned: Any) -> Optional[frozenset]:
    """dlz (build C1): ``Hermes.paymentSurfaceHolders`` as the holder ids of the claim of ``tab``, or None (not
    answered, another claim, any other shape). Exactly ``{tabs: [tab], holders}``, holders distinct well-formed
    ids, none the claimed tab, every one in the claim reply's owning set ``owned``."""
    if not isinstance(result, dict) or set(result) != {"tabs", "holders"} or result["tabs"] != [tab]:
        return None
    holders = result["holders"]
    if not (isinstance(holders, list) and len(holders) <= _MAX_STATUS_TABS and all(_valid_id(h) for h in holders)
            and len(set(holders)) == len(holders)):
        return None
    if tab in holders or not set(holders) <= set(owned):
        return None
    return frozenset(holders)


def _claim_lock(supervisor: Any) -> threading.Lock:
    with _LOCK:
        lock = _CLAIM_LOCKS.get(supervisor)
        if lock is None:
            lock = _CLAIM_LOCKS[supervisor] = threading.Lock()
        return lock


def _claiming(supervisor: Any) -> bool:
    """Must hold ``_LOCK``."""
    return _CLAIMING.get(supervisor, 0) > 0


def claim_payment_surface(supervisor: Any, inspected: Any) -> Optional[dict]:
    """The fill path's hook (managed mode, after consent, BEFORE ``resolve_secret``).

    Returns None when control confirmed this supervisor's claim of the payment tab; otherwise the
    refusal result to return as-is (``payment_surface_unavailable``). Never retried: a refusal means
    another connection holds the slot or control cannot verify the tab, and the fill must stop with
    nothing resolved. The local lock is set (and the epoch bumped) BEFORE the claim is sent, so a
    relayed call whose reply arrives during or after the claim is withheld.
    """
    loop = getattr(supervisor, "_loop", None)
    tab = getattr(inspected, "page_id", None)
    if loop is None or not loop.is_running() or not _valid_id(tab):
        return _refusal(UNAVAILABLE, _UNAVAILABLE_TEXT)
    lock = _claim_lock(supervisor)
    if not lock.acquire(timeout=_CLAIM_WAIT_S):  # another claim or a fenced native op is running
        return _refusal(UNAVAILABLE, _UNAVAILABLE_TEXT)
    try:
        if getattr(supervisor, "_stop_requested", False):  # dlz (J-0001): its socket is closing (stop_for_turn_end)
            return _refusal(UNAVAILABLE, _UNAVAILABLE_TEXT)
        return _claim_serialized(supervisor, inspected, loop, tab)
    finally:
        lock.release()


def stop_for_turn_end(supervisor: Any) -> bool:
    """dlz (J-0001): the turn end's decision to stop ``supervisor``, exclusive with claim admission. Under its claim
    lock (not waited for: a running claim, dialog answer or recovery call keeps it), True when it is in no payment
    (nothing locally locked, no record not handed over) and marks it stopping, so every later claim on it refuses
    before anything is sent; False keeps it."""
    from tools.browser_payment_done import has_payment

    lock = _claim_lock(supervisor)
    if not lock.acquire(blocking=False):
        return False
    try:
        if locally_locked(supervisor) or has_payment(supervisor):
            return False
        supervisor._stop_requested = True
        return True
    finally:
        lock.release()


def _claim_serialized(supervisor: Any, inspected: Any, loop: Any, tab: str) -> Optional[dict]:
    gen = None
    with _LOCK:
        held_before = supervisor in _HELD
        old = _GENS.get(supervisor) if held_before else None
        pressing_at_start = _PRESSING_N > 0
        if not held_before:  # a new lock of this process (pressed_in_lock)
            gen = _GENS[supervisor] = _LockGen(known=not pressing_at_start and supervisor not in _KNOWN_LOCKED)
        elif old is not None:  # B1: a new fill is a new approval; only its success binds a total
            old.approved = None
            old.admitted = False  # r4 C7: and no press of it was admitted yet
        _HELD.add(supervisor)
        _CLAIMING[supervisor] = _CLAIMING.get(supervisor, 0) + 1
        _EPOCHS[supervisor] = _EPOCHS.get(supervisor, 0) + 1
    _note_claim(supervisor)
    ws = getattr(supervisor, "_ws", None)
    try:
        reply = _wait(supervisor._cdp(CLAIM_METHOD, {"tabs": [tab]}, timeout=_CLAIM_TIMEOUT_S), loop,
                      timeout=_CLAIM_TIMEOUT_S + 5)
        result = reply.get("result") if isinstance(reply, dict) else None
        if not parse_claim_result(result, [tab]) or getattr(supervisor, "_ws", None) is not ws:
            raise SurfaceRefused("claim not confirmed")
    except Exception as exc:  # health: allow BLE001 -- CDP/transport text is not surfaced; fixed type only
        if _control_refused(exc) and not held_before:
            # Only control's own refusal proves nothing changed; any other failure keeps the lock here,
            # and so does another claim still on the wire.
            with _LOCK:
                if _CLAIMING.get(supervisor, 0) <= 1:
                    _HELD.discard(supervisor)
                    if gen is not None and _GENS.get(supervisor) is gen:
                        _GENS.pop(supervisor, None)
        logger.warning("card fill refused: payment surface claim failed (%s)", type(exc).__name__)
        return _refusal(UNAVAILABLE, _UNAVAILABLE_TEXT)
    finally:
        with _LOCK:
            left = _CLAIMING.get(supervisor, 0) - 1
            if left > 0:
                _CLAIMING[supervisor] = left
            else:
                _CLAIMING.pop(supervisor, None)
    owned = frozenset(result["tabs"])
    others = owned - {tab}
    if others:  # build C1: control's own holder of the claimed page is part of this claim, not another page
        holders = _claim_holders(supervisor, loop, tab, owned, ws)
        others = others - holders if holders is not None else others
    _start_record(supervisor, gen, old, held_before, pressing_at_start, owned, bool(others), ws,
                  _merchant_identity(inspected))
    # dlz (lockdown §2): the payment tab and its card documents, for browser_vault payment_done.
    from tools.browser_payment_done import record_payment

    record_payment(supervisor, inspected)
    return None


def _claim_holders(supervisor: Any, loop: Any, tab: str, owned: frozenset, ws: Any) -> Optional[frozenset]:
    """dlz (build C1): the tab-type holders control recorded with this claim (``HOLDERS_METHOD``), or None when it
    cannot say (an older control answers ``cdp_method_blocked``; a timeout, another socket): the extra ids then
    count as other tabs in the lock, as before (fail closed)."""
    try:
        reply = _wait(supervisor._cdp(HOLDERS_METHOD, {}, timeout=5), loop, timeout=7)
    except Exception as exc:  # health: allow BLE001 -- old control or no answer: unknown, fail closed
        logger.info("payment lock holders not named by control (%s)", type(exc).__name__)
        return None
    if getattr(supervisor, "_ws", None) is not ws:
        return None
    return parse_holders(reply.get("result") if isinstance(reply, dict) else None, tab, owned)


def _start_record(supervisor: Any, gen: Any, old: Any, held_before: bool, pressing_at_start: bool,
                  owned: frozenset, others: bool, ws: Any, merchant: Optional[tuple] = None) -> None:
    """After a confirmed claim: the lock record this fill presses in. A claim while this process held no lock keeps
    the record it started before the claim was sent (``gen``). Build C2: a claim while it held one (a new fill after
    an unproven release, or a re-fill) starts a fresh record, so presses of an earlier record never carry over; that
    record is unknown when the old lock may still be held (control's owning set still names one of its tabs, or the
    old record never had a confirmed claim) or a click was running when the claim began; build C r2 (P7): when the old
    lock still holds and its record was known, the new record carries its presses instead. Either is unknown when
    the owning set holds a page that is not this claim's tab or its holder (``others``). build C r4: ``merchant``
    (``_merchant_identity``) is kept with the record."""
    global _RECORD_SEQ
    with _LOCK:
        if not held_before:
            if gen is None or _GENS.get(supervisor) is not gen:
                return  # the lock ended (or was replaced) while the claim was on the wire
            if others:
                gen.known = False
            rec, replaced = gen, None
        else:
            if _GENS.get(supervisor) is not old:
                return  # another claim's record already replaced it
            still_held = old is None or old.owned is None or bool(owned & old.owned)
            # build C r2 (P7): a re-fill while the old lock still holds carries the old count when the old record
            # was known (its presses are presses in this lock); an unknown old record stays unknown
            carry = (still_held and old is not None and old.owned is not None and old.known
                     and bool(owned & old.owned))
            rec = _GENS[supervisor] = _LockGen(known=not (pressing_at_start or others or (still_held and not carry)))
            if carry:
                rec.presses, rec.pressed = old.presses, old.pressed
            replaced = old.serial if old is not None else 0
        rec.owned, rec.ws, rec.merchant = owned, ws, merchant
        _RECORD_SEQ += 1
        if rec.serial == 0:
            rec.serial = _RECORD_SEQ
        known, serial, n = rec.known, rec.serial, len(owned)
    if replaced is None:
        logger.info("payment lock record %d started (owning ids %d, presses known %s)", serial, n, known)
    else:
        logger.info("payment lock record %d replaces record %d (owning ids %d, presses known %s)",
                    serial, replaced, n, known)


def _valid_checkout(top: Any, merchant: Any) -> bool:
    """``top`` is a ``(frame id, loader)`` pair and ``merchant`` an https origin."""
    return (isinstance(top, tuple) and len(top) == 2 and _valid_id(top[0]) and isinstance(top[1], str)
            and _ID_RE.fullmatch(top[1]) is not None and isinstance(merchant, str) and merchant.startswith("https://")
            and _origin_of(merchant) == merchant)


def _merchant_identity(inspected: Any) -> Optional[tuple]:
    """build C r4: ``(top frame id, loader, merchant origin)`` of the checkout a claim is for (the inspection the fill
    runs on), or None when it cannot be read."""
    try:
        top, merchant = tuple(inspected.tree[0][:2]), inspected.merchant_origin
    except (AttributeError, IndexError, TypeError):
        return None
    return (*top, merchant) if _valid_checkout(top, merchant) else None


def _note_claim(supervisor: Any) -> None:
    """dlz: a claim fences every payment-window release that observed the slot before it (any supervisor)."""
    try:
        from tools.browser_payment_card_values import note_claim

        note_claim(supervisor)
    except Exception as exc:  # health: allow BLE001 -- unfenced: a stale release can only narrow masking it reopens at fill
        logger.info("payment window generation not bumped (%s)", type(exc).__name__)


def _observed_windows(supervisor: Any) -> dict:
    try:
        from tools.browser_payment_card_values import observe_windows

        return observe_windows(supervisor)
    except Exception:  # health: allow BLE001 -- nothing observed: nothing is released
        return {}


def _slot_bound(supervisor: Any) -> bool:
    try:
        from tools.browser_payment_card_values import slot_bound

        return slot_bound(supervisor)
    except Exception:  # health: allow BLE001 -- unknown binding: this read releases nothing it did not lock
        return False


def lock_status(supervisor: Any) -> str:
    """Control's answer for this supervisor's slot: ``locked`` | ``unlocked`` | ``unknown``. A read that
    straddles a claim (one on the wire at its start or end, or started meanwhile) is ``locked``: only a
    clean unlock clears the local lock or counts as a release, and it releases only the payment windows
    whose generation it observed before the read (a claim by any supervisor on the slot since then wins).
    dlz: the unlock counts whoever saw the lock: a supervisor that never did (it replaced the paying one, or
    the tab was closed by someone else before its first read) releases its slot's window too, when its socket
    is the one its route names (``slot_bound``, checked before and after the read)."""
    observed = _observed_windows(supervisor)
    bound = _slot_bound(supervisor)
    with _LOCK:
        start, busy = _EPOCHS.get(supervisor, 0), _claiming(supervisor)
    native = None
    try:
        reply = _wait(supervisor._cdp(STATUS_METHOD, {}, timeout=5), supervisor._loop, timeout=7)
        state, _tabs, native = parse_lock_status_native(reply.get("result") if isinstance(reply, dict) else None)
    except Exception:  # health: allow BLE001 -- an unanswered status proves nothing
        state = "unknown"
    unlocked = saw_lock = False
    with _LOCK:
        if busy or _claiming(supervisor) or _EPOCHS.get(supervisor, 0) != start:
            return "locked"
        if state == "unlocked":
            unlocked, saw_lock = True, supervisor in _HELD or supervisor in _KNOWN_LOCKED
            _HELD.discard(supervisor)
            _GENS.pop(supervisor, None)  # the lock ended
            _KNOWN_LOCKED.discard(supervisor)
            _NOT_NATIVE.discard(supervisor)
        elif state == "locked":
            _KNOWN_LOCKED.add(supervisor)
            if native is False:  # build C4: the lock is held, by another connection than this socket
                _NOT_NATIVE.add(supervisor)
                gen = _GENS.get(supervisor) if supervisor in _HELD else None
                if gen is not None and gen.owned is not None:
                    gen.native_lost = True
            else:
                _NOT_NATIVE.discard(supervisor)
    # dlz: the confirmed release narrows the payment window to browser results (card_values)
    if unlocked and (saw_lock or (bound and _slot_bound(supervisor))):
        try:
            from tools.browser_payment_card_values import release_payment_window

            release_payment_window(supervisor, observed)
        except Exception as exc:  # health: allow BLE001 -- window kept: more masking, never less
            logger.info("payment window not closed (%s)", type(exc).__name__)
    return state


def _managed() -> bool:
    try:
        from tools.browser_vault_tool import _is_managed

        return bool(_is_managed())
    except Exception:  # health: allow BLE001 -- unknown mode: treat as managed (fail closed)
        return True


def slot_locked(supervisor: Any) -> bool:
    """Whether model-reachable calls on this supervisor's (native) socket must be refused. Locked when
    a claim from this process is not proven released, when control says locked, and on a managed
    browser whenever control's answer is unknown. A browser without browser-control (no lock exists)
    is unlocked unless this process claimed it."""
    return _lock_verdict(supervisor) != "unlocked"


def _lock_verdict(supervisor: Any) -> str:
    """:func:`slot_locked` as ``locked`` | ``unlocked`` | ``unknown`` (build C5): ``unknown`` only when control's
    answer is unknown on a managed browser and this process holds no claim and saw no lock on this supervisor, so
    nothing says a payment is running; it still refuses every model-reachable call."""
    start = epoch(supervisor)
    state = lock_status(supervisor)
    with _LOCK:
        if _claiming(supervisor) or _EPOCHS.get(supervisor, 0) != start:
            return "locked"
        held, seen = supervisor in _HELD, supervisor in _KNOWN_LOCKED
    if state == "unknown":
        if held or seen:
            return "locked"
        return "unknown" if _managed() else "unlocked"
    return state


def locally_locked(supervisor: Any) -> bool:
    """For loop-side code that must not wait for a status read (dialog automation): a claim on the wire,
    a claim not proven released, or control's last answer locked."""
    with _LOCK:
        return _claiming(supervisor) or supervisor in _HELD or supervisor in _KNOWN_LOCKED


NATIVE_LOST = "payment_native_lost"
NATIVE_LOST_TEXT = (
    "Hermes lost its own browser connection while this card payment held the lock (the connection the card fill "
    "claimed the payment tab on was replaced), so it can no longer list, click or read the payment tab, and whether "
    "Pay went through is unknown. Do not press anything, do not fill the card again and do not pay again. Tell the "
    "owner (Danil) to check the order, the shop's e-mail and his bank. Call browser_vault payment_close only after "
    "he has looked: it closes the payment tab and ends the lock.")


def claim_socket_changed(supervisor: Any) -> bool:
    """build C4 (loop-safe, local state only): this supervisor holds a lock record whose confirmed claim went out on
    another socket than its current one. Control's native connection is the claim's socket, so a redialled socket
    is never native while that lock holds."""
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        return (gen is not None and gen.owned is not None and gen.ws is not None
                and getattr(supervisor, "_ws", None) is not gen.ws)


def lock_status_seen(supervisor: Any, native: bool) -> None:
    """r2 P5 (loop side): control's status, read by the supervisor on a fresh socket that holds no claim of this
    process, said locked (``native``: whether this socket is the lock's native connection)."""
    with _LOCK:
        if supervisor in _HELD or _claiming(supervisor):
            return  # a claim of this process: only lock_status books its state
        _KNOWN_LOCKED.add(supervisor)
        if native is False:
            _NOT_NATIVE.add(supervisor)


def mark_native_lost(supervisor: Any) -> None:
    """build C4: control named this lock held by another connection than this supervisor's socket."""
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        if gen is not None and gen.owned is not None:
            gen.native_lost = True


def native_lost_refusal(supervisor: Any) -> Optional[dict]:
    """build C4: ``payment_native_lost`` for a native payment action (payment_controls / payment_click /
    payment_done / payment_dialog) when its lock can no longer be served on this socket, else None:
    - this process's lock record says so (its claim socket was replaced, or control reported the lock held by
      another connection) and control does not prove the slot unlocked; or
    - build C r2 (P6): this process holds no claim on this supervisor (a replaced supervisor, a restarted gateway)
      and control reports the slot locked with this socket not native: ``payment_lock_foreign`` instead.
    An unlocked slot is never "lost": the action then answers as it does without a payment."""
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        recorded = gen is not None and gen.owned is not None
        flagged = recorded and gen.native_lost
    if recorded and not flagged and not claim_socket_changed(supervisor):
        return None
    if not recorded:
        from tools.browser_payment_done import has_payment

        if has_payment(supervisor):  # a payment this process recorded on this supervisor: not a foreign lock
            return None
    state = lock_status(supervisor)
    if state == "unlocked":
        return None
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        if recorded:
            if gen is not None and gen.owned is not None:
                gen.native_lost = True
            return _refusal(NATIVE_LOST, NATIVE_LOST_TEXT)
        foreign = state == "locked" and supervisor in _NOT_NATIVE and gen is None
    return _refusal(LOCK_FOREIGN, LOCK_FOREIGN_TEXT) if foreign else None


LOCK_FOREIGN = "payment_lock_foreign"
LOCK_FOREIGN_TEXT = (
    "Another connection holds a card-payment lock on this browser (not a payment this Hermes task started or knows), "
    "so nothing was read or pressed. Do not press anything and do not close it unless Danil says so: tell him "
    "(it may be a payment still waiting for its confirmation).")


def fenced_native(supervisor: Any, op) -> dict:
    """Run a model-initiated native operation (a dialog answer) outside any claim: under the supervisor's
    claim lock (a claim waits for it), refused while the slot is locked, and its payload withheld if a claim
    started anyway (the epoch moved)."""
    lock = _claim_lock(supervisor)
    if not lock.acquire(timeout=_FENCE_WAIT_S):
        return _refusal(LOCKED, LOCKED_TEXT)
    try:
        before = epoch(supervisor)
        refused = locked_refusal(supervisor)
        if refused:
            return refused
        out = op()
        return locked_withheld() if epoch(supervisor) != before else out
    finally:
        lock.release()


def epoch(supervisor: Any) -> int:
    """This supervisor's claim count (see ``_EPOCHS``)."""
    with _LOCK:
        return _EPOCHS.get(supervisor, 0)


def fenced_dispatch(supervisor: Any, before: int, call) -> tuple:
    """One native dispatch of a long, non-card operation (a vault page op that may wait on the user between
    dispatches): under the claim lock, only while the slot is unlocked and no claim happened since ``before``
    (this supervisor's epoch when the operation started). Returns ``("ok", result)`` or ``("refused",
    refusal)``; nothing is dispatched when refused."""
    lock = _claim_lock(supervisor)
    if not lock.acquire(timeout=_FENCE_WAIT_S):
        return "refused", _refusal(LOCKED, LOCKED_TEXT)
    try:
        if epoch(supervisor) != before:
            return "refused", locked_withheld()
        refused = vault_lock_refusal(supervisor)
        if refused:
            return "refused", refused
        return "ok", call()
    finally:
        lock.release()


CONNECTION_UNAVAILABLE = "browser_connection_unavailable"
_CONNECTION_UNAVAILABLE_TEXT = (
    "Hermes could not read the browser's payment-lock state (browser control did not answer), and no card payment "
    "of this Hermes process is open on it, so nothing was read or written. Retry this once; if it fails again, "
    "report it to the user. This is not a payment lock: do not call payment_close for it.")


def vault_lock_refusal(supervisor: Any) -> Optional[dict]:
    """build C5: the refusal for a non-card vault page operation (login, signup, code fills), else None.
    ``payment_surface_locked`` while the slot is locked; ``browser_connection_unavailable`` (retry once) when the
    lock state is unknown and this process holds no claim and saw no lock. Either way nothing is dispatched."""
    verdict = _lock_verdict(supervisor)
    if verdict == "unknown":
        return _refusal(CONNECTION_UNAVAILABLE, _CONNECTION_UNAVAILABLE_TEXT)
    return _refusal(LOCKED, LOCKED_TEXT) if verdict == "locked" else None


def locked_refusal(supervisor: Any, method: str = "", session_id: Optional[str] = None) -> Optional[dict]:
    """``payment_surface_locked`` for a model-reachable call while the slot is locked, else None.
    ``method``/``session_id`` name a relayed CDP command (``locked.commands`` without a session pass);
    a non-CDP action (dialog answer, vault page operation) passes neither and is refused while locked."""
    if method in LOCKED_COMMANDS and not session_id:
        return None
    return _refusal(LOCKED, LOCKED_TEXT) if slot_locked(supervisor) else None


def locked_withheld() -> dict:
    """A relayed call whose reply arrived after a card payment claimed the slot: never returned."""
    return _refusal(LOCKED, "A card payment locked this browser while the call was running, so its result was "
                            "withheld. " + LOCKED_TEXT)


RECOVERY_METHODS = ("Target.getTargets", "Target.closeTarget")


def _origin_only(url: Any) -> str:
    from tools.browser_payment_done import _shown_origin

    return _shown_origin(url)


async def _page_tabs(sup: Any) -> list:
    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    return [t for t in infos if isinstance(t, dict) and t.get("type") == "page" and _valid_id(t.get("targetId"))]


def recovery_call(supervisor: Any, method: str, params: Any) -> dict:
    """While the slot is locked, the managed, lease-bound way out for the model: ``Target.getTargets`` lists
    this slot's tabs with their site only (origin, empty title), ``Target.closeTarget {targetId}`` closes one
    of them (closing the payment tab is what ends the lock). Native, under the claim lock; anything else,
    or other parameters, is refused."""
    params = params if params is not None else {}
    if method == "Target.getTargets" and params != {}:
        return _refusal(LOCKED, "While locked, Target.getTargets takes no parameters.")
    if method == "Target.closeTarget" and not (isinstance(params, dict) and set(params) == {"targetId"}
                                               and _valid_id(params["targetId"])):
        return _refusal(LOCKED, "While locked, Target.closeTarget takes exactly {targetId} of a tab from "
                                "Target.getTargets.")
    if method not in RECOVERY_METHODS:
        return _refusal(LOCKED, LOCKED_TEXT)
    lock = _claim_lock(supervisor)
    if not lock.acquire(timeout=_FENCE_WAIT_S):
        return _refusal(LOCKED, LOCKED_TEXT)
    try:
        tabs = _wait(_page_tabs(supervisor), supervisor._loop, timeout=10)
        if method == "Target.getTargets":
            return {"success": True, "method": method, "result": {"targetInfos": [
                {"targetId": t["targetId"], "type": "page", "url": _origin_only(t.get("url")), "title": ""}
                for t in tabs]}}
        if params["targetId"] not in {t["targetId"] for t in tabs}:
            return _refusal(LOCKED, "That is not a tab of this browser; list them with Target.getTargets.")
        _wait(supervisor._cdp("Target.closeTarget", {"targetId": params["targetId"]}), supervisor._loop, timeout=10)
        from tools.browser_payment_done import _forget

        _forget(supervisor, params["targetId"])  # payment_done no longer waits for (or counts) that tab
        # No status read here: control's watcher needs targetDestroyed and a re-read before it says unlocked, so a
        # read straight after the close usually still says locked and only adds time under the claim lock. The
        # window is released by the next reconcile or status read (any later browser call, or control's view).
        return {"success": True, "method": method, "result": {}}
    except Exception as exc:  # health: allow BLE001 -- CDP text is not surfaced; fixed type only
        logger.info("locked recovery call failed (%s)", type(exc).__name__)
        return _refusal(LOCKED, "The browser could not answer; nothing else was done. Ask the user.")
    finally:
        lock.release()


def supervisor_route_refusal(supervisor: Any, frame_id: str, method: str, params: Any,
                             session_id: Optional[str] = None) -> Optional[dict]:
    """Refusal for ``browser_cdp(frame_id=…)`` on the native socket (``session_id``: where it lands),
    or None to send it."""
    if str(method or "").lower().startswith("hermes."):
        return _refusal(LOCKED, "browser-control methods cannot be sent through browser_cdp.")
    return locked_refusal(supervisor, str(method or ""), session_id)


# ------------------------------------------------------------ native actions

# Runs in a fresh isolated world of one protected document. Lists clickable controls and never an
# editable field: text inputs, selects, textareas and contenteditable are skipped, and so is any
# control that contains one or labels one. Returns the elements array (by reference) so the caller
# describes or clicks by index without the page seeing the call.
_LISTER_SRC = r"""(() => {
  const EDIT = 'input:not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]),select,textarea,[contenteditable=""],[contenteditable=true]';
  const SEL = 'button,a[href],[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],summary,'
    + 'input[type=checkbox],input[type=radio],input[type=submit],input[type=button]';
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (el.matches(EDIT) || el.querySelector(EDIT)) continue;
    if (el.tagName === 'LABEL' && el.control && el.control.matches(EDIT)) continue;
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility !== 'visible' || st.display === 'none') continue;
    out.push(el);
    if (out.length >= %d) break;
  }
  return out;
})()"""
_LISTER = _LISTER_SRC.replace("%d", str(_MAX_CONTROLS))
_TOP_LISTER = _LISTER_SRC.replace("%d", str(_MAX_TOP_CONTROLS))

_DESCRIBE = r"""function(){ return this.map((el, i) => {
  const t = (el.type || '').toLowerCase(); const role = (el.getAttribute('role') || '').toLowerCase();
  const kind = (t === 'checkbox' || role === 'checkbox') ? 'checkbox' : (t === 'radio' || role === 'radio') ? 'radio'
    : (el.tagName === 'A' || role === 'link') ? 'link' : 'button';
  const input = el.tagName === 'INPUT' && (t === 'submit' || t === 'button' || t === 'reset');
  const raw = el.getAttribute('aria-label') || (input ? el.value : el.innerText) || el.title || '';
  return {i, role: kind, text: String(raw).replace(/\s+/g, ' ').trim(),
          disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true')};
}); }"""

_HIT = r"""function(){ this.scrollIntoView({block: 'center', inline: 'center'});
  const r = this.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y); return !!hit && (hit === this || this.contains(hit)); }"""


# build C3: structural, count-only reads around a press (nothing but bools, counts and a class word leaves the world).
_OCCLUDER_JS = r"""const occluderClass = el => {
  if (!el) return 'other';
  if (el.closest && el.closest('__DIALOG__')) return 'dialog';
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    const p = getComputedStyle(n).position; if (p === 'fixed' || p === 'sticky') return 'fixed_overlay';
  }
  return 'other';
};""".replace("__DIALOG__", _DIALOG_SEL)
# On the control (its own document): the class of what sits at its centre.
_OCCLUDER = ("function(){ " + _OCCLUDER_JS + r"""
  const r = this.getBoundingClientRect();
  return occluderClass(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)); }""")
# In the payment tab's top document, at page coordinates (an element over a card iframe).
_OCCLUDER_AT = "((x, y) => { " + _OCCLUDER_JS + " return occluderClass(document.elementFromPoint(x, y)); })"
# On the pressed control: still listed (connected, laid out, visible), still hit at its centre, its checked state.
_CONTROL_STATE = r"""function(){ const r = this.getBoundingClientRect(); const st = getComputedStyle(this);
  const listed = this.isConnected && r.width >= 1 && r.height >= 1 && st.visibility === 'visible' && st.display !== 'none';
  let hit = false;
  if (listed) { const h = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                hit = !!h && (h === this || this.contains(h)); }
  return {listed, hit, checked: !!this.checked || this.getAttribute('aria-checked') === 'true',
          disabled: !!this.disabled || this.getAttribute('aria-disabled') === 'true',
          busy: this.getAttribute('aria-busy') === 'true'}; }"""
# The top document's open dialogs and large fixed overlays (a count, at most 50 + 5).
_OVERLAYS = r"""(() => {
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width >= 1 && r.height >= 1 && st.visibility === 'visible' && st.display !== 'none' && st.opacity !== '0'; };
  let n = 0;
  for (const el of document.querySelectorAll('__DIALOG__')) { if (vis(el) && ++n >= 50) break; }
  const W = innerWidth, H = innerHeight, seen = new Set();
  for (const [fx, fy] of [[.5, .5], [.25, .25], [.75, .25], [.25, .75], [.75, .75]]) {
    for (let el = document.elementFromPoint(W * fx, H * fy); el && el !== document.body && el !== document.documentElement;
         el = el.parentElement) {
      if (getComputedStyle(el).position !== 'fixed') continue;
      const r = el.getBoundingClientRect();
      if (r.width * r.height >= 0.25 * W * H && !el.matches('__DIALOG__')) seen.add(el);
      break;
    }
  }
  return n + seen.size;
})()""".replace("__DIALOG__", _DIALOG_SEL)
_OCCLUDER_CLASSES = ("dialog", "fixed_overlay", "other")
_SETTLE_S = 1.5  # build C3: the longest a click waits after its release before it reads the effect
_SETTLE_POLL_S = 0.25
_EFFECT_BUDGET_S = 3.0  # settle plus the effect reads; the press margin leaves the caller more than this


def _covered_text(occluder: str) -> str:
    what = {"dialog": "a dialog", "fixed_overlay": "a fixed overlay"}.get(occluder, "another element")
    return f"Another element covers that control ({what}); nothing was clicked."


def _covered(occluder: Optional[str]) -> dict:
    """``payment_control_covered`` with the occluder's class only (``dialog`` | ``fixed_overlay`` | ``other``); None:
    without a class (build C r2 P4: a document in the lock could read the filled card values)."""
    if occluder is None:
        return _refusal("payment_control_covered", "Another element covers that control; nothing was clicked.")
    occluder = occluder if occluder in _OCCLUDER_CLASSES else "other"
    return {**_refusal("payment_control_covered", _covered_text(occluder)), "occluder": occluder}


async def _covered_at(sup: Any, sid: str, element: str, page_sid: str, tree: tuple, child_sessions: dict,
                      card: bool = False) -> dict:
    """build C r2 (P4): ``payment_control_covered``; the occluder class is read only when no document in the lock can
    read the filled card values (:func:`detail_safe` on the listing's frame tree), else not read at all. build C r4
    (Codex low 3): never for a control inside a hosted card frame (``card``): the provider's document could encode a
    card-derived bit in what covers its own control."""
    if card or not detail_safe(sup, [(r[0], r[2], r[3]) for r in tree]):
        return _covered(None)
    return _covered(await _occluder(sup, sid, element, page_sid, tree, child_sessions))


def detail_safe(sup: Any, frames: list) -> bool:
    """build C r2 (P4, Codex P1): whether page-controlled metadata (overlay and card-frame counts, the occluder class)
    may leave the payment tab. ``frames``: ``(frame id, url, security origin)`` of the payment tab, the top document
    first. Only when every card document of this supervisor's payments lives in a hosted frame cross-origin to the
    top document (no merchant card field, every card frame in the tree on another origin) and the top document is
    not a card document (not one of their frames, not on a provider or card-receiving origin). Otherwise a document
    that can read the filled values could encode them in counts or classes. A card frame missing from ``frames`` is
    an out-of-process frame, cross-site to the top by site isolation; one listed without a readable origin is a doubt.
    Any doubt: False."""
    try:
        from tools import browser_payment_done as done

        with done._LOCK:
            payments = list((done._PAYMENTS.get(sup) or {}).values())
        if not payments or not frames or any(p.merchant_fields for p in payments):
            return False

        def origin(row: tuple) -> str:
            return row[2] if isinstance(row[2], str) and row[2] else _origin_of(row[1])

        top = origin(frames[0])
        cards = {fid for p in payments for fid in p.frame_ids}
        if not top or frames[0][0] in cards or any(top in done._card_origins(p) for p in payments):
            return False
        return all(origin(row) not in ("", top) for row in frames[1:] if row[0] in cards)
    except Exception as exc:  # health: allow BLE001 -- unknown: no page-controlled metadata
        logger.info("payment effect detail withheld (%s)", type(exc).__name__)
        return False


def _tree_rows(tree: Any) -> list:
    """``Page.getFrameTree``'s ``frameTree`` as ``(frame id, url, security origin)`` rows, the top first."""
    rows, stack = [], [tree]
    while stack:
        node = stack.pop(0)
        frame = node.get("frame") or {}
        rows.append((frame.get("id"), frame.get("url"), frame.get("securityOrigin") or ""))
        stack.extend(node.get("childFrames") or [])
    return rows


async def _occluder(sup: Any, sid: str, element: str, page_sid: str, tree: tuple, child_sessions: dict) -> str:
    """build C3: the class of what covers the control: read in the control's own document first, else (a page element
    over its card iframe) in the payment tab's top document at the control's page point. Any failure: ``other``."""
    try:
        own = (await sup._cdp("Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _OCCLUDER,
                                                         "returnByValue": True}, session_id=sid))["result"]["result"]
        hit = (await sup._cdp("Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _HIT,
                                                         "returnByValue": True}, session_id=sid))["result"]["result"]
        if hit.get("value") is not True or sid == page_sid:
            return own.get("value") if own.get("value") in _OCCLUDER_CLASSES else "other"
        x, y, _outer = await _page_point(sup, sid, element, page_sid, tree, child_sessions)
        ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": tree[0][0], "worldName": "hermes-payment-effect"},
                              session_id=page_sid))["result"]["executionContextId"]
        at = (await sup._cdp("Runtime.evaluate", {"expression": f"{_OCCLUDER_AT}({float(x)!r}, {float(y)!r})",
                                                  "contextId": ctx, "returnByValue": True},
                             session_id=page_sid))["result"]["result"]
        return at.get("value") if at.get("value") in _OCCLUDER_CLASSES else "other"
    except Exception as exc:  # health: allow BLE001 -- an unread class is "other"; the refusal stands
        logger.info("payment_control_covered: occluder not classified (%s)", type(exc).__name__)
        return "other"


async def _structure(sup: Any, page_sid: str, element: str, sid: str) -> dict:
    """build C3: the counts and states an effect compares (top document, the control, and only when
    :func:`detail_safe`, overlays and card frames; build C r2 P4: never read otherwise)."""
    from tools.browser_payment_done import card_frame_ids

    tree = (await sup._cdp("Page.getFrameTree", session_id=page_sid))["result"]["frameTree"]
    rows = _tree_rows(tree)
    top = (tree.get("frame") or {}).get("id"), (tree.get("frame") or {}).get("loaderId")
    out: dict = {"top": tuple(top), "safe": detail_safe(sup, rows), "overlays": None, "card_frames": None}
    try:
        state = (await sup._cdp("Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _CONTROL_STATE,
                                                           "returnByValue": True}, session_id=sid))["result"]["result"]
        value = state.get("value") if isinstance(state.get("value"), dict) else {}
    except Exception:  # health: allow BLE001 -- the control's document is gone: not listed, not hit
        value = {}
    out.update(listed=value.get("listed") is True, hit=value.get("hit") is True,
               state=tuple(value.get(k) is True for k in ("checked", "disabled", "busy")))
    if not out["safe"]:
        return out
    ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": top[0], "worldName": "hermes-payment-effect"},
                          session_id=page_sid))["result"]["executionContextId"]
    n = (await sup._cdp("Runtime.evaluate", {"expression": _OVERLAYS, "contextId": ctx, "returnByValue": True},
                        session_id=page_sid))["result"]["result"].get("value")
    if tuple((await _top_frame(sup, page_sid))[:2]) != out["top"]:  # another document answered: not judged safe
        out["safe"] = False
        return out
    out["overlays"] = n if type(n) is int and 0 <= n <= 1000 else None
    frames = {row[0] for row in rows}
    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    frames |= {t.get("targetId") for t in infos if isinstance(t, dict) and t.get("type") == "iframe"}
    out["card_frames"] = len(card_frame_ids(sup) & frames)
    return out


async def _effect(sup: Any, page_sid: str, element: str, sid: str, before: Optional[dict], role: str) -> Optional[dict]:
    """build C3: what the click visibly did, counts and yes/no only, read after a bounded settle (at most
    ``_SETTLE_S``; earlier once the top document changed). None when it cannot be read."""
    if before is None:
        return None
    end = time.monotonic() + _SETTLE_S
    while time.monotonic() < end:
        await asyncio.sleep(min(_SETTLE_POLL_S, max(0.0, end - time.monotonic())))
        try:
            if tuple((await _top_frame(sup, page_sid))[:2]) != before["top"]:
                break
        except Exception:  # health: allow BLE001 -- a navigating page may not answer yet; keep settling
            continue
    after = await _structure(sup, page_sid, element, sid)
    navigated = after["top"] != before["top"]
    # build C r2 (P3): the clicked control's own checked / disabled / aria-busy state, before vs after
    changed = after["state"] != before["state"]
    effect = {"navigated": navigated, "control_listed": after["listed"], "control_hit": after["hit"],
              "control_state_changed": changed}
    still = not navigated and after["listed"] and after["hit"] and not changed
    if before["safe"] and after["safe"]:  # build C r2 (P4): page-controlled counts only from safe documents
        overlays = before["overlays"], after["overlays"]
        effect.update(overlays_before=overlays[0], overlays_after=overlays[1],
                      card_frames_before=before["card_frames"], card_frames_after=after["card_frames"])
        if role in {"checkbox", "radio"}:
            effect["toggled"] = after["state"][0] != before["state"][0]
        still = (still and None not in overlays and overlays[0] == overlays[1]
                 and before["card_frames"] == after["card_frames"])
    effect["unchanged"] = still
    return effect


async def _bounded_effect(sup: Any, page_sid: str, element: str, sid: str, before: Optional[dict], role: str,
                          deadline: float) -> Optional[dict]:
    """The effect read within ``_EFFECT_BUDGET_S`` and well before the caller's own timeout; a read that fails or
    runs out is None (the click's success stands: it was pressed)."""
    budget = min(_EFFECT_BUDGET_S, deadline + _PRESS_MARGIN_S - 1.5 - time.monotonic())
    if budget <= 0:
        return None
    try:
        return await asyncio.wait_for(_effect(sup, page_sid, element, sid, before, role), timeout=budget)
    except Exception as exc:  # health: allow BLE001 -- unread effect: None, never an uncertain click
        logger.info("payment_click: effect not read (%s)", type(exc).__name__)
        return None


def _mask(text: str) -> str:
    return re.sub(r"\d{4,}", lambda m: "•" * len(m.group(0)), text)[:_MAX_TEXT]


def _caption(text: str, sup: Any) -> str:
    """A control caption (text/aria-label/title — anything ``_DESCRIBE`` reads) for the model: the
    supervisor-scoped dialog redactor first, so every 3+ digit run, date-like group and amount but an
    owner-approved total is masked and this slot's registered card values (holder included) are scrubbed —
    a page that echoes the card into a caption cannot pass it on. Applied BEFORE the ref is built, so refs
    bind the masked text. The redactor's own digit mask already covers what ``_mask`` would; re-masking
    would only hide an approved total it deliberately kept, so just the ``_MAX_TEXT`` bound remains. When
    the redactor cannot run, the caption is withheld (a fixed text: no digit, letter or name passes)."""
    try:
        from tools.browser_payment_done import redact_dialog_text

        return redact_dialog_text(text, sup)[:_MAX_TEXT]
    except Exception:  # health: allow BLE001 -- redactor unavailable: withhold, never a weaker mask
        return CAPTION_WITHHELD


def _ref(key: str, index: int, role: str, text: str, nonce: str = "") -> str:
    """A control's ref: bound to its document (``key`` = frame id + loader) and to the listing (``nonce``)."""
    digest = hashlib.sha256(f"{key}|{index}|{role}|{text}|{nonce}".encode()).hexdigest()[:10]
    return f"c{index}-{digest}"


# The refs of each supervisor's latest payment_controls listing: ref -> (document key, index, role, text).
# A new listing replaces them; a press (successful or uncertain) consumes its ref.
_ISSUED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def _loader_of(sup: Any, sid: str, fid: str) -> Optional[str]:
    """The loader of frame ``fid``'s current document, read from its session's frame tree (None: gone)."""
    stack = [(await sup._cdp("Page.getFrameTree", session_id=sid))["result"]["frameTree"]]
    while stack:
        node = stack.pop()
        frame = node.get("frame") or {}
        if frame.get("id") == fid:
            loader = frame.get("loaderId")
            return loader if isinstance(loader, str) and loader else None
        stack.extend(node.get("childFrames") or [])
    return None


async def _same_document(sup: Any, sid: str, fid: str, loader: str) -> None:
    if await _loader_of(sup, sid, fid) != loader:
        raise SurfaceRefused("the page changed")


async def _documents(sup: Any) -> tuple:
    with sup._state_lock:
        page_sid = sup._page_session_id if sup._active else None
    if not page_sid:
        raise SurfaceRefused("supervised browser required")
    child_sessions: dict = {}
    tree = await sup._payment_tree(page_sid, child_sessions)
    from tools.browser_payment_done import card_frame_ids, top_document

    # The payment's card documents (claimed by this supervisor), then, since under the slot lock agent CDP
    # cannot click anything, the payment tab's own top document (merchant page, 3-D Secure redirect) unless
    # it is a card frame.
    frames = card_frame_ids(sup)
    # (frame id, session, document key = frame id + loader, loader, top document?)
    docs = [(f[0], child_sessions.get(f[0], page_sid), f"{f[0]}|{f[1]}", f[1], f is tree[0])
            for f in tree if f[0] in frames and isinstance(f[1], str) and f[1]]
    top = await top_document(sup, page_sid, tree)
    if top is not None and top[0] not in {d[0] for d in docs}:
        docs.append((*top, tree[0][1], True))
    return page_sid, tree, child_sessions, docs


async def _listing(sup: Any, fid: str, sid: str, key: str, loader: str, top: bool, nonce: str) -> tuple:
    """(array object id, [control dicts]) for one listed document. The isolated world is checked to belong
    to the document that was judged (its loader read again after the listing)."""
    ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": fid, "worldName": "hermes-payment-controls"},
                          session_id=sid))["result"]["executionContextId"]
    arr = (await sup._cdp("Runtime.evaluate", {"expression": _TOP_LISTER if top else _LISTER, "contextId": ctx,
                                               "returnByValue": False}, session_id=sid))["result"]["result"].get(
                                                   "objectId")
    if not arr:
        raise SurfaceRefused("payment document unavailable")
    try:
        rows = (await sup._cdp("Runtime.callFunctionOn", {"objectId": arr, "functionDeclaration": _DESCRIBE,
                                                          "returnByValue": True}, session_id=sid))["result"]["result"]
        await _same_document(sup, sid, fid, loader)
    except BaseException:
        await sup._release_payment_object(sid, arr)
        raise
    controls = []
    for row in rows.get("value") or []:
        text = _caption(str(row.get("text") or ""), sup)
        role = row.get("role") if row.get("role") in {"button", "link", "checkbox", "radio"} else "button"
        index = int(row.get("i") or 0)
        controls.append({"ref": _ref(key, index, role, text, nonce), "role": role, "text": text,
                         "disabled": row.get("disabled") is True, "_i": index, "_spec": (key, index, role, text)})
    return arr, controls


async def _list_all(sup: Any, nonce: str = "") -> tuple:
    page_sid, tree, child_sessions, docs = await _documents(sup)
    listed = []
    try:
        for fid, sid, key, loader, top in docs:
            arr, controls = await _listing(sup, fid, sid, key, loader, top, nonce)
            listed.append((fid, sid, arr, controls, loader))
    except BaseException:
        await _release(sup, child_sessions, listed)
        raise
    return page_sid, tree, child_sessions, listed


async def _release(sup: Any, child_sessions: dict, listed: list) -> None:
    for _fid, sid, arr, _c, _loader in listed:
        await sup._release_payment_object(sid, arr)
    await sup._release_payment_sessions(child_sessions)


def _public(controls: list) -> list:
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in controls]


async def _controls(sup: Any) -> dict:
    nonce = secrets.token_hex(8)
    _page_sid, _tree, child_sessions, listed = await _list_all(sup, nonce)
    try:
        controls = [c for _f, _s, _a, cs, _l in listed for c in cs]
        with _LOCK:
            _ISSUED[sup] = {c["ref"]: c["_spec"] for c in controls}
        return {"success": True, "documents": len(listed), "controls": _public(controls)}
    finally:
        await _release(sup, child_sessions, listed)


def _owner_target(sid: str, page_sid: str, child_sessions: dict, iframes: set) -> Optional[str]:
    if sid == page_sid:
        return None
    owners = [k for k, v in child_sessions.items() if v == sid and k in iframes]
    if len(owners) != 1:
        raise SurfaceRefused("payment document owner unknown")
    return owners[0]


async def _page_point(sup: Any, sid: str, element: str, page_sid: str, tree: tuple,
                      child_sessions: dict) -> tuple:
    """The element's centre in page coordinates, plus the outermost OOPIF owner node (or None)."""
    quads = (await sup._cdp("DOM.getContentQuads", {"objectId": element}, session_id=sid))["result"]["quads"]
    if not quads:
        raise SurfaceRefused("control not rendered")
    q = quads[0]
    x, y = sum(q[0::2]) / 4, sum(q[1::2]) / 4
    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    iframes = {t["targetId"] for t in infos if t.get("type") == "iframe"}
    parents = {f[0]: f[4] for f in tree}
    outer = None
    target = _owner_target(sid, page_sid, child_sessions, iframes)
    while target is not None:
        parent = parents.get(target)
        if not parent:
            raise SurfaceRefused("payment frame owner unknown")
        parent_sid = child_sessions.get(parent, page_sid)
        owner = (await sup._cdp("DOM.getFrameOwner", {"frameId": target}, session_id=parent_sid))["result"]
        box = (await sup._cdp("DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]},
                              session_id=parent_sid))["result"]["model"]["content"]
        x, y = x + box[0], y + box[1]
        if parent_sid == page_sid:
            outer = owner["backendNodeId"]
        target = _owner_target(parent_sid, page_sid, child_sessions, iframes)
    return x, y, outer


async def _target_point(sup: Any, sid: str, element: str, page_sid: str, tree: tuple,
                        child_sessions: dict) -> tuple:
    """The control's centre in page coordinates and its outermost OOPIF owner, or ``(None, None, None)`` when
    another element (inside the frame chain, read on the page) sits at that point."""
    x, y, outer = await _page_point(sup, sid, element, page_sid, tree, child_sessions)
    if outer is not None:
        at = (await sup._cdp("DOM.getNodeForLocation", {"x": round(x), "y": round(y)},
                             session_id=page_sid))["result"].get("backendNodeId")
        if at != outer:
            return None, None, None
    return x, y, outer


async def _click(sup: Any, spec: tuple, progress: list, deadline: float, gate: _Gate) -> dict:
    """``gate``: what payment_click admitted the click with (B1, ``_pay_gate``; r4 C2: the claim generation)."""
    from tools import browser_payment_detect as detect

    if gate.approved is UNBOUND:  # r4 C8: the card covered Pay but its total is not bound: never the owner_go path
        return _refusal(TOTAL_UNBOUND, _TOTAL_UNBOUND_TEXT)
    if gate.approved is None and not gate.owner_go:
        return _refusal(GO_REQUIRED, _GO_REQUIRED_TEXT)  # before anything is read on the page
    page_sid, tree, child_sessions, listed = await _list_all(sup)
    element = None
    sid = ""
    try:
        found = [(fid, sid, arr, c, loader) for fid, sid, arr, cs, loader in listed for c in cs if c["_spec"] == spec]
        if len(found) != 1:
            return _refusal("payment_control_unknown", "That control is not listed now; call payment_controls again.")
        fid, sid, arr, control, loader = found[0]
        if control["disabled"]:
            return _refusal("payment_control_disabled", "That control is disabled; nothing was clicked.")
        # build G1 (owner 2026-11-19): any number of presses while the approval is open; a Pay-like press (not a
        # checkbox, radio or a clearly preparatory caption) is followed by the payment detection, and is refused only
        # once a payment was detected or while the last Pay-like press's navigation is still loading.
        pay_like = _pay_press(control["role"], control["text"])
        if pay_like and (spent := detect.spent_refusal(gate.gen)):
            return spent  # nothing was read on the page
        row = next((r for r in tree if r[0] == fid), None)
        clicked = (fid, loader, row[2] if row else "", fid == tree[0][0])
        element = await _element(sup, sid, arr, control["_i"])
        card = not clicked[3]  # build C r4: a control in a hosted card frame never names its occluder's class
        refused, x, y, outer = await _aim(sup, sid, element, page_sid, tree, child_sessions, card)
        if refused:
            return refused
        # The move first (hover handlers may re-render or navigate), then everything is read again: the
        # document, the hit target, the owner chain and the point. Any change: no press.
        await sup._cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session_id=page_sid)
        await _same_document(sup, sid, fid, loader)
        refused, x2, y2, outer2 = await _target_holds(sup, sid, element, page_sid, tree, child_sessions, (x, y, outer),
                                                      card=card)
        if refused:
            return refused
        await _same_document(sup, sid, fid, loader)
        top = None
        if gate.approved is not None:  # B1: the approved total, read before every press
            refused, top = await _pay_gate(sup, page_sid, gate, clicked, element, sid, pay_like)
            if refused:
                return refused
        kept = None
        if pay_like:  # build G1/G2: still loading, a payment shown now; else the booleans kept with this press
            refused, kept = await detect.before_press(sup, gate, page_sid, deadline)
            if refused:
                return refused
        node = await pay_node(sup, sid, element, clicked) if pay_like else None
        tabs = await detect.page_tabs(sup) if pay_like else None
        before = await _baseline(sup, page_sid, element, sid)
        refused = await _press_now(sup, deadline, (page_sid, sid, fid, loader, element, tree, child_sessions),
                                   (x2, y2, outer2), top, lambda: _book_press(
                                       sup, gate, gate.approved is not None, pay_like, progress, node=node,
                                       caption=_caption(control["text"], sup), top=tree[0][0], base=kept),
                                   gate.approved is not None,
                                   last=(lambda: detect.last_check(sup, gate, page_sid, kept)) if pay_like else None)
        if refused:
            return refused
        effect = await _bounded_effect(sup, page_sid, element, sid, before, control["role"], deadline)
        detected = (await detect.after_press(sup, gate, page_sid, tabs, _caption(control["text"], sup),
                                             deadline + _PRESS_MARGIN_S - 1.0, kept) if pay_like else False)
        return _clicked_answer(sup, control, effect, pay_like, detected)
    finally:
        if element:
            await sup._release_payment_object(sid, element)
        await _release(sup, child_sessions, listed)


async def _element(sup: Any, sid: str, arr: str, index: int) -> str:
    """The listed control's element (by its index in the listing's array, in the controls' isolated world)."""
    element = (await sup._cdp("Runtime.callFunctionOn", {
        "objectId": arr, "functionDeclaration": "function(i){return this[i]}",
        "arguments": [{"value": index}], "returnByValue": False}, session_id=sid))["result"]["result"].get("objectId")
    if not element:
        raise SurfaceRefused("control gone")
    return element


async def _aim(sup: Any, sid: str, element: str, page_sid: str, tree: tuple, child_sessions: dict,
               card: bool) -> tuple:
    """``(refusal or None, x, y, outer)``: the control is hit at its centre and nothing in its frame chain covers
    that point."""
    hit = (await sup._cdp("Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _HIT,
                                                     "returnByValue": True}, session_id=sid))["result"]["result"]
    if hit.get("value") is not True:
        return await _covered_at(sup, sid, element, page_sid, tree, child_sessions, card), 0, 0, 0
    x, y, outer = await _target_point(sup, sid, element, page_sid, tree, child_sessions)
    if x is None:
        return await _covered_at(sup, sid, element, page_sid, tree, child_sessions, card), 0, 0, 0
    return None, x, y, outer


async def _baseline(sup: Any, page_sid: str, element: str, sid: str) -> Optional[dict]:
    """build C3: what the effect is compared with (counts and states only; never a reason to refuse)."""
    try:
        return await _structure(sup, page_sid, element, sid)
    except Exception as exc:  # health: allow BLE001 -- no baseline: the click goes on, its effect is null
        logger.info("payment_click: structure before the press not read (%s)", type(exc).__name__)
        return None


async def _press_now(sup: Any, deadline: float, doc: tuple, point: tuple, top: Optional[tuple], book,
                     checked: bool, last=None) -> Optional[dict]:
    """r4 C2/C3: from here to the press no claim can start (the claim lock is held), and everything the gate's awaits
    could have let change is read once more: the document, the hit target, the owner chain and point, the top
    document the gate judged, and (last, with no await before the press, ``book``: ``_book_press``) the claim
    generation. ``doc``: ``(page session, session, frame id, loader, element, tree, child sessions)``; ``checked``: the
    press was checked against an approved total (else an owner_go press). A refusal means nothing was pressed."""
    page_sid, sid, fid, loader, element, tree, child_sessions = doc
    fence = await _fence(sup, deadline)
    try:
        await _same_document(sup, sid, fid, loader)
        refused, _x, _y, _o = await _target_holds(sup, sid, element, page_sid, tree, child_sessions, point,
                                                  card=fid != tree[0][0])
        if refused:
            return refused
        if top is not None and (await _top_frame(sup, page_sid))[:2] != top[:2]:
            return _refusal(TOTAL_CHANGED, _TOTAL_CHANGED_TEXT) if checked else _changed()
        if last is not None and (refused := await last()):  # build F r6: the last await before the press
            return refused
        # Nothing awaits between these checks and the dispatch: past the deadline the caller may already have
        # reported "nothing was clicked", so the press must not start.
        if time.monotonic() >= deadline:
            raise SurfaceRefused("the action ran out of time before the click")
        # build C r2 (P2): admission re-checked and the press booked in one step under _LOCK, at dispatch: a listing
        # from now on counts it. From here a failure cannot promise the click did not land (duplicate-submit risk).
        refused = book()
        if refused:
            return refused
        for kind in ("mousePressed", "mouseReleased"):
            await sup._cdp("Input.dispatchMouseEvent", {"type": kind, "x": point[0], "y": point[1], "button": "left",
                                                        "clickCount": 1}, session_id=page_sid)
    finally:
        fence.release()
    return None


def _clicked_answer(sup: Any, control: dict, effect: Optional[dict], pay_like: bool, detected: bool = False) -> dict:
    out = {"success": True, "role": control["role"], "text": _caption(control["text"], sup), "effect": effect}
    if pay_like:  # build C r2 (P3): what a Pay-like press's page did never reads as "Pay failed"
        if effect is not None:
            effect["unchanged"] = None
        out.update(pay_pressed=True, payment_detected=detected, next=DETECTED_NEXT if detected else PAY_PRESSED_NEXT)
    return out


async def _target_holds(sup: Any, sid: str, element: str, page_sid: str, tree: tuple, child_sessions: dict,
                        point: tuple, *, card: bool = False) -> tuple:
    """``(refusal or None, x, y, outer)``: the control is still the element at its hit-tested centre and the point and
    its OOPIF owner are the ones measured before (``point``), within a pixel. ``card``: the control is in a hosted card
    frame (``_covered_at``)."""
    hit = (await sup._cdp("Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _HIT,
                                                     "returnByValue": True}, session_id=sid))["result"]["result"]
    if hit.get("value") is not True:
        return await _covered_at(sup, sid, element, page_sid, tree, child_sessions, card), 0, 0, 0
    x, y, outer = await _target_point(sup, sid, element, page_sid, tree, child_sessions)
    if x is None or outer != point[2] or abs(x - point[0]) > 1 or abs(y - point[1]) > 1:
        return _changed(), 0, 0, 0
    return None, x, y, outer


def _changed() -> dict:
    return _refusal("payment_control_changed", "The control moved or changed just before the click; nothing was "
                                               "pressed. Call payment_controls again.")


# r4 C2: how long a click waits for the claim lock (a claim, dialog answer or recovery call running) before it
# refuses with nothing pressed.
_FENCE_CLICK_WAIT_S = 3.0


async def _fence(sup: Any, deadline: float) -> threading.Lock:
    """The supervisor's claim lock, taken on the loop without blocking it (a claim holding it waits on this loop)."""
    lock = _claim_lock(sup)
    until = min(deadline, time.monotonic() + _FENCE_CLICK_WAIT_S)
    while not lock.acquire(blocking=False):
        if time.monotonic() >= until:
            raise SurfaceRefused("another browser operation was running")
        await asyncio.sleep(0.02)
    return lock


def _admission_holds(sup: Any, gate: _Gate) -> bool:
    """r4 C2: no claim (a new fill, so a new approval) started since the click was admitted, and the lock and the
    approval it was admitted under are still the current ones. Must hold ``_LOCK``."""
    if _EPOCHS.get(sup, 0) != gate.epoch or _claiming(sup) or _CLOSING_N > 0:
        return False
    if gate.gen is None:  # no lock of this process at admission (owner_go): a claim since would show above
        return True
    return sup in _HELD and _GENS.get(sup) is gate.gen and gate.gen.approved is gate.approved


def _book_press(sup: Any, gate: _Gate, checked: bool, pay_press: bool, progress: list, *, node: Any = None,
                caption: str = "", top: Any = None, base: Optional[dict] = None) -> Optional[dict]:
    """build C r2 (P2): right before mousePressed, in one step under ``_LOCK``: the admission still holds (r4 C2) and,
    for a Pay-like press, no payment was detected for this approval (build G1); then the press is booked in its lock
    record (``presses``, ``pressed``, r4 C7 ``admitted``), so a listing during the press or its settle already counts
    it. A refusal (nothing booked) means nothing is pressed. build G1: a Pay-like press keeps its time and ``top``
    frame id (``pay_at``, the in-flight guard) and, on that top document or as the first one, ``node`` ``(frame id,
    loader, backendNodeId, origin + path)`` and its redacted ``caption`` (``pay_press``: payment_done's identity
    veto); ``base``: the document and strong-wording boolean read right before the press (``pay_base``, build G2).
    ``gen`` is None when this process saw no lock of this supervisor: nothing to book then."""
    with _LOCK:
        if not _admission_holds(sup, gate):
            return _refusal(APPROVAL_CHANGED, _APPROVAL_CHANGED_TEXT)
        gen = gate.gen
        if pay_press and gen is not None and gen.paid:
            from tools.browser_payment_detect import spent_refusal

            return spent_refusal(gen)
        progress.extend(("checked" if checked else "go", _BOOKED))
        if gen is not None:  # the admission above proved it is still this supervisor's current record
            gen.pressed = True
            gen.presses += 1
            if checked:  # r4 C7: a press checked against this approval (not owner_go)
                gen.admitted = True
            if pay_press:
                gen.pay_at, gen.pay_base = (top, time.monotonic()), base
                if (node and node[0] == top) or gen.pay_node is None:
                    gen.pay_node, gen.pay_caption = node, caption
    return None


_BOOKED = object()  # in ``progress``: the press was booked at its dispatch
PAY_PRESSED_NEXT = (
    "Read the page. If it shows an order confirmation, call payment_done. If it shows another step (a confirm, "
    "3-D Secure, a dialog), continue.")
DETECTED_NEXT = (
    "The page shows the order was placed (payment_detected): call payment_done now. Do not press Pay or Buy again: a "
    "new purchase needs a new approval from the owner (Danil).")
CLICK_BUSY = "payment_click_busy"
_CLICK_BUSY_TEXT = (
    "Another payment_click is still running (its press, or the short wait after it), so nothing was pressed. Wait "
    "for its result, then call payment_controls again.")
TOTAL_CHANGED = "payment_total_changed"
GO_REQUIRED = "payment_go_required"
TOTAL_UNBOUND = "payment_total_unbound"
APPROVAL_CHANGED = "payment_approval_changed"
_TOTAL_CHANGED_TEXT = (
    "Hermes could not confirm that the merchant's checkout page still shows the total the owner approved (it changed, "
    "is no longer clear, or the checkout page is not the one that was filled), so nothing was pressed and the lock "
    "stays. Stop: do not press Pay or anything else, do not retry, and tell the owner (Danil) what you see. A "
    "different total needs a new approval: payment_close, then fill again with the current cart.")
_GO_REQUIRED_TEXT = (
    "Hermes holds no approved total for Pay in this payment lock, so nothing was pressed. Either the card was fill-only "
    "(no stated total, or the page check did not match it), Pay-once approvals are switched off "
    "(vault.payment_pay_once), or this lock's approval is not known to this Hermes process (after a restart or a "
    "replaced browser connection). Ask the owner (Danil) in chat; only after his explicit go for this purchase call "
    "payment_click again with owner_go true.")
_TOTAL_UNBOUND_TEXT = (
    "The owner's card covered one Pay press, but Hermes could not bind its approved total to this payment lock, so it "
    "cannot check the total before the press. Nothing was pressed, and owner_go does not change this. Stop and tell "
    "the owner (Danil); to go on, payment_close, then fill again (a new approval card).")
_APPROVAL_CHANGED_TEXT = (
    "A new card fill started on this browser while the click was being checked, so the approval it was checked "
    "against no longer holds. Nothing was pressed. Call payment_controls again; the new fill's card decides whether "
    "Pay is covered.")


# r4 C1 (Codex r3 P1-1, peer r3 P2-2): the Pay check, stricter than the fill's page check and read with it at every
# checked press. A token is a number (thousands groups with . , ' or a no-break space; an optional 2-digit or '.-'
# decimal part); its unit is an ISO 4217 code or a currency symbol (up to 3 letters before it) right beside it on
# either side, as the fill's check reads units; a 3-capital word that is no ISO code is no unit. An amount is a token
# with decimals or a unit. Refused: any token whose unit is not the approved currency, anywhere in the text; the
# first amount after any total label (the fill's labels plus Endbetrag, Zahlbetrag, "amount to be charged", "you will
# be charged" and more) within 120 characters and 4 line breaks (a label and its amount on separate lines), or with
# none there the last amount before the label on its own line, when it is not the approved total; the first amount
# within 16 characters after a pay word on its line ("Pay 120.00 EUR", "Jetzt bezahlen 120,00 €") when it is not the
# approved total; a text over 200000 characters. Only ``{ok, why}`` leaves the world.
_PAY_TOKENS = r"""
  const CODES = new Set('__CODES__'.split(' '));
  const DOLLARS = {'': 'USD', US: 'USD', CA: 'CAD', C: 'CAD', A: 'AUD', AU: 'AUD', NZ: 'NZD'};
  const NUM = /(?<![\p{N}])(\d{1,3}(?:[.,'’  ]\d{3})+|\d{1,9})(?:[.,](\d{2})(?![\p{N}])|[.,]([-–]{1,2}))?(?![\p{N}])/gu;
  const LEFT = /(?<![\p{L}\p{N}])(?:[A-Z]{3}|\p{L}{0,3}\p{Sc})$/u, RIGHT = /^(?:[A-Z]{3}(?![\p{L}\p{N}])|\p{L}{0,3}\p{Sc})/u;
  const unitOf = m => {
    if (/^[A-Z]{3}$/.test(m)) return CODES.has(m) ? m : null;
    const cs = Array.from(m), symbol = cs.pop(), prefix = cs.join('');
    if (symbol === '$') return DOLLARS[prefix] || '?';
    return prefix ? '?' : symbol === '€' ? 'EUR' : symbol === '£' ? 'GBP' : '?';
  };
  const tokens = text => {
    const out = [];
    for (const m of text.matchAll(NUM)) {
      const start = m.index, end = start + m[0].length;
      const a = text.slice(Math.max(0, start - 12), start), b = text.slice(end, end + 12);
      const left = a.slice(Math.max(a.lastIndexOf('\n'), a.lastIndexOf('\r')) + 1).trimEnd();
      const right = b.split(/[\n\r]/)[0].trimStart();
      const lu = LEFT.exec(left), ru = RIGHT.exec(right);
      const units = [lu && unitOf(lu[0]), ru && unitOf(ru[0])].filter(u => u);
      const value = Number(m[1].replace(/\D/g, '')) * 100 + (m[2] ? Number(m[2]) : 0);
      out.push({start, end, value, units, amount: !!(m[2] || m[3]) || units.length > 0});
    }
    return out;
  };
  const same = (t, cents, currency) => t.value === cents && t.units.every(u => u === currency);
"""
_PAY_PAGE_SRC = r"""((cents, currency) => {
""" + _PAY_TOKENS + r"""
  const text = String((document.body && document.body.innerText) || '');
  if (text.length > 200000) return {ok: false, why: 'size'};
  const all = tokens(text), amounts = all.filter(t => t.amount);
  if (all.some(t => t.units.some(u => u !== currency))) return {ok: false, why: 'currency'};
  const TOTAL = /(?<![\p{L}\p{N}])(gesamtbetrag|gesamtsumme|gesamtpreis|totalbetrag|endbetrag|endpreis|endsumme|zahlbetrag|rechnungsbetrag|zu bezahlen|zu zahlen|gesamt|summe|betrag|order total|grand total|total due|total to pay|total amount|amount due|amount to pay|amount to be charged|you will be charged|you'll be charged|will be charged|to be charged|you pay|montant total|montant|totale|importe total|importe|total)(?![\p{L}\p{N}])/giu;
  for (const k of text.matchAll(TOTAL)) {
    const from = k.index + k[0].length;
    const after = amounts.find(t => t.start >= from);
    if (after && after.start - from <= 120 && (text.slice(from, after.start).match(/[\n\r]/g) || []).length <= 4) {
      if (!same(after, cents, currency)) return {ok: false, why: 'label'};
      continue;
    }
    const line = Math.max(text.lastIndexOf('\n', k.index - 1), text.lastIndexOf('\r', k.index - 1)) + 1;
    const before = amounts.filter(t => t.end <= k.index && t.start >= line).pop();
    if (before && !same(before, cents, currency)) return {ok: false, why: 'label'};
  }
  const PAY = /(?<![\p{L}\p{N}])(jetzt bezahlen|jetzt zahlen|zahlungspflichtig bestellen|kostenpflichtig bestellen|jetzt kaufen|bezahlen|zahlen|kaufen|confirm and pay|pay now|place order|buy now|book now|pay|buy|payer|pagar|paga)(?![\p{L}\p{N}])/giu;
  for (const k of text.matchAll(PAY)) {
    const from = k.index + k[0].length;
    const after = amounts.find(t => t.start >= from);
    if (after && after.start - from <= 16 && !/[\n\r]/.test(text.slice(from, after.start))
        && !same(after, cents, currency)) return {ok: false, why: 'pay'};
  }
  return {ok: true, why: 'ok'};
})"""
# The pressed control's captions: no amount but the approved total, no other currency (a bool leaves the world).
_CAPTION_CHECK_SRC = r"""function(cents, currency) {
""" + _PAY_TOKENS + r"""
  const texts = [this.getAttribute ? this.getAttribute('aria-label') : '', this.innerText, this.value, this.title];
  for (const raw of texts) {
    if (typeof raw !== 'string' || !raw) continue;
    for (const t of tokens(raw.slice(0, 2000))) {
      if (t.units.some(u => u !== currency) || (t.amount && t.value !== cents)) return false;
    }
  }
  return true;
}"""
_PAY_JS: dict = {}


def _pay_js(name: str) -> str:
    """``_PAY_PAGE_SRC`` / ``_CAPTION_CHECK_SRC`` with the fill's ISO 4217 list (read from ``_AMOUNT_WITNESS``)."""
    if not _PAY_JS:
        from tools.browser_supervisor_payment import _AMOUNT_WITNESS

        codes = re.search(r"new Set\('([A-Z]{3}(?: [A-Z]{3})+)'\.split", _AMOUNT_WITNESS).group(1)
        _PAY_JS.update(page=_PAY_PAGE_SRC.replace("__CODES__", codes),
                       caption=_CAPTION_CHECK_SRC.replace("__CODES__", codes))
    return _PAY_JS[name]


class _Unbound:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNBOUND"


# r4 C8: the lock's approval covered Pay, but its total could not be bound (``_LockGen.approved``).
UNBOUND = _Unbound()


class _Gate(NamedTuple):
    """What payment_click admitted a click with, under ``_LOCK``: the lock's approval (tuple, None or ``UNBOUND``),
    whether a press checked against it was admitted (C7), the owner's go, and (r4 C2) the claim generation."""
    approved: Any
    admitted: bool
    owner_go: bool
    epoch: int
    gen: Any


def record_approved_total(supervisor: Any, cents: Any, currency: Any, top: Any, merchant: Any) -> bool:
    """B1: after a successful card fill whose card stated the total and whose page check matched it, bind that total
    (integer cents, ISO code), the merchant top document it was read in (``(frame id, loader)``) and the merchant
    origin to the current lock: every payment_click in this lock reads it again before its press. Nothing is bound
    without a lock of this process; a malformed value binds ``UNBOUND`` (r4 C8: every press refused). True when the
    total was bound."""
    good = (type(cents) is int and 0 <= cents < 10**9 and isinstance(currency, str)
            and re.fullmatch(r"[A-Z]{3}", currency) is not None and _valid_checkout(top, merchant))
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        if gen is None:
            return False
        gen.approved = (cents, currency, top[0], top[1], merchant) if good else UNBOUND
        return good


def mark_unbound(supervisor: Any) -> None:
    """r4 C8: a fill whose card covered Pay could not bind its total: this lock's presses are refused
    (``payment_total_unbound``), never left to the owner_go path."""
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        if gen is not None:
            gen.approved = UNBOUND


def _origin_of(url: Any) -> str:
    from tools.browser_payment_done import _url_origin

    return _url_origin(url)


def approved_total(supervisor: Any) -> Optional[tuple]:
    """``(cents, currency)`` bound to this supervisor's current lock, or None."""
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        return gen.approved[:2] if gen is not None and isinstance(gen.approved, tuple) else None


async def _page_total(sup: Any, sid: str, fid: str, cents: int, currency: str) -> tuple:
    """The fill's amount check (``_AMOUNT_WITNESS``, the same parser and rules) and the Pay check (``_PAY_PAGE``)
    read once in a fresh isolated world of document ``fid``: only their verdicts (state words, numbers, a bool) leave
    the world, nothing stays pinned."""
    from tools.browser_supervisor_payment import _AMOUNT_WITNESS

    ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": fid, "worldName": "hermes-payment-total"},
                          session_id=sid))["result"]["executionContextId"]
    args = "(" + str(cents) + "," + json.dumps(currency) + ")"
    reply = await sup._cdp("Runtime.evaluate", {
        "expression": "(" + _AMOUNT_WITNESS + ")" + args + ".pin()", "contextId": ctx, "returnByValue": True},
        session_id=sid)
    pin = ((reply.get("result") or {}).get("result") or {}).get("value")
    reply = await sup._cdp("Runtime.evaluate", {"expression": "(" + _pay_js("page") + ")" + args, "contextId": ctx,
                                                "returnByValue": True}, session_id=sid)
    check = ((reply.get("result") or {}).get("result") or {}).get("value")
    return pin, isinstance(check, dict) and check.get("ok") is True


async def _caption_holds(sup: Any, sid: str, element: str, cents: int, currency: str) -> bool:
    """r4 C1: the pressed control's own captions (aria-label, text, value, title) hold no amount but the approved
    one and no other currency (read in the controls' isolated world; only a bool leaves it)."""
    reply = await sup._cdp("Runtime.callFunctionOn", {
        "objectId": element, "functionDeclaration": _pay_js("caption"), "arguments": [{"value": cents}, {"value": currency}],
        "returnByValue": True}, session_id=sid)
    return ((reply.get("result") or {}).get("result") or {}).get("value") is True


async def _top_frame(sup: Any, sid: str) -> tuple:
    """``(frame id, loader, url)`` of the page's current top document."""
    frame = (await sup._cdp("Page.getFrameTree", session_id=sid))["result"]["frameTree"].get("frame") or {}
    return frame.get("id"), frame.get("loaderId"), frame.get("url")


async def _pay_gate(sup: Any, page_sid: str, gate: _Gate, clicked: tuple, element: str, sid: str,
                    pay_like: bool = False) -> tuple:
    """B1 (owner 2026-11-13): ``(None, top)`` when this press may go out, else ``(payment_total_changed, top)``.
    ``clicked``: the pressed control's document ``(frame id, loader, url, is the top)``.

    Every press: the control's own caption holds no amount but the approved total (r4 C1), and the top document is
    still the payment tab the fill bound (r4 C4: same top frame). The first press of an approval needs the bound
    merchant top document (same frame and loader). On it, and after an admitted press on any later top document that
    is same-site with the merchant or whose site is unknown (a review page), the approved total must be ``matched`` by
    the fill's own rule and pass the Pay check (``_PAY_PAGE``), the document read again after the check. After an
    admitted press, a top document on another site (a bank's 3-D Secure page, r4 C4) and a bank challenge that
    replaced one of the payment's card documents under the bound checkout (r4 C5) have no merchant total: they are
    pressed with the caption check only. build G2 (Codex P1-1): a Pay-like press (``pay_like``) on a top document on a
    payment provider's or card origin (the fill's frames moved there after Pay) is no bank step: it keeps the total
    check, as in the live build (where a provider's page after Pay was refused outright)."""
    from tools.browser_supervisor_payment import _page_match, _valid_pin

    cents, currency, fid, loader, merchant = gate.approved
    top = await _top_frame(sup, page_sid)
    no = _refusal(TOTAL_CHANGED, _TOTAL_CHANGED_TEXT)
    if top[0] != fid or not await _caption_holds(sup, sid, element, cents, currency):
        return no, top
    if top[1] == loader and gate.admitted and _bank_challenge(sup, clicked, merchant):
        return None, top
    if top[1] != loader:
        if not gate.admitted:
            return no, top
        if cross_site(top[2], merchant) and not (pay_like and _provider_site(sup, top[2])):
            return None, top
    pin, page_ok = await _page_total(sup, page_sid, top[0], cents, currency)
    if (await _top_frame(sup, page_sid))[:2] != top[:2]:  # the document changed during the read
        return no, top
    if not page_ok or not _valid_pin(pin) or _page_match([("merchant page", pin)], cents)[0] != "matched":
        return no, top
    return None, top


def _bank_challenge(sup: Any, clicked: tuple, merchant: str) -> bool:
    """r4 C5: the pressed document is a bank challenge in place of a card document: a frame that held one of this
    payment's card documents now shows another document (new loader) on a site that is not the merchant's, and not on
    a payment provider's or card-receiving origin's site (build C r4; a provider's own next page keeps the merchant
    total check)."""
    fid, loader, url, is_top = clicked
    if is_top:
        return False
    from tools import browser_payment_done as done

    with done._LOCK:
        payments = list((done._PAYMENTS.get(sup) or {}).values())
    documents = {d for p in payments for d in p.documents}
    return (fid in {d[0] for d in documents} and (fid, loader) not in documents and cross_site(url, merchant)
            and not _provider_site(sup, url))


def _run(supervisor: Any, coro_factory, progress: list | None = None) -> dict:
    from tools.browser_payment_done import has_payment

    lost = native_lost_refusal(supervisor)  # build C4: before anything is read or pressed
    if lost:
        return lost
    if not has_payment(supervisor):
        return _refusal("payment_surface_none", "No card payment by Hermes is open on this browser connection: "
                                                "use the browser tools as usual.")
    # Taken before the wait starts, so the press deadline always falls before the caller's timeout.
    deadline = time.monotonic() + _ACTION_TIMEOUT_S - _PRESS_MARGIN_S
    try:
        return _wait(coro_factory(deadline), supervisor._loop, timeout=_ACTION_TIMEOUT_S)
    except SurfaceRefused as exc:
        if progress:
            return _uncertain_click()
        return _refusal(UNAVAILABLE, f"The payment page could not be used ({exc}); nothing was clicked.")
    except Exception as exc:  # health: allow BLE001 -- CDP text can quote the page; fixed type only
        logger.info("payment surface action failed: %s", type(exc).__name__)
        if progress:
            return _uncertain_click()
        return _refusal(UNAVAILABLE, "The payment page could not be read by the native action; nothing "
                                     "was clicked. If the native browser connection was replaced, finish on the "
                                     "merchant page or ask the user.")


def _uncertain_click() -> dict:
    return _refusal("payment_click_uncertain", "Outcome uncertain: a press may have landed (the click was being "
                    "sent when the action failed or ran out of time). Do NOT click again or retry; call "
                    "browser_vault payment_done to see where the payment tab ended up, or ask the user whether the "
                    "purchase went through.")


def _with_dialog(supervisor: Any, out: dict) -> dict:
    """dlz: a dialog the lock holds for an answer is reported (bounded, digits masked); see payment_dialog."""
    from tools.browser_payment_done import _dialog_report

    return {**out, **_dialog_report(supervisor)}


def payment_controls(supervisor: Any) -> dict:
    """``browser_vault action=payment_controls``: ``{ref, role, text, disabled}`` per control."""
    out = _with_dialog(supervisor, _run(supervisor, lambda _deadline: _controls(supervisor)))
    return {**out, "presses_in_lock": presses_in_lock(supervisor)}


# dlz (Codex r3 P1, r4 P1): payment_click / payment_close admission, process-wide under _LOCK (not per supervisor:
# a supervisor replaced after a reconnect drives the same browser slot). A close is admitted only with no click
# running anywhere in this process, and a click only with no close admitted anywhere. build C r3 (Q3): a click is
# refused ``payment_click_busy`` only by another click on the same supervisor (``_PRESSING``).
_PRESSING_N = 0  # payment_click calls running (their press may be on the wire)
# build C r3 (Q3): the same, per supervisor (one browser slot): two tasks on two browsers never refuse each other
_PRESSING: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_CLOSING_N = 0  # payment_close calls admitted and not yet returned


class _LockGen:
    """dlz (Codex r4 P2, Opus r4 P2-1): one lock this process started (a claim while it held none). ``pressed``: a
    payment_click admitted during this lock started a press (or may have). ``known``: this process saw the whole
    lock, so ``pressed`` False means no press. Unknown when the lock began while a click was still running (its
    press may have landed in it), when control already reported the slot locked, or when control's lock already
    held other tabs. Read and written under ``_LOCK``."""

    __slots__ = ("admitted", "approved", "known", "merchant", "native_lost", "owned", "paid", "paid_by", "pay_at",
                 "pay_base", "pay_caption", "pay_node", "pay_tabs", "pressed", "presses", "serial", "ws")

    def __init__(self, known: bool):
        self.known, self.pressed, self.presses = known, False, 0  # presses: admitted presses (a press may count twice never)
        # build C2: control's owning set at this record's confirmed claim (None: no confirmed claim yet), and a
        # process-local serial for the log (ids only)
        self.owned: Optional[frozenset] = None
        self.serial = 0
        # build C4: the supervisor socket the claim went out on (control's native connection), and whether control
        # has since reported this lock held by another connection (``native: false``)
        self.ws: Any = None
        self.native_lost = False
        # B1: (cents, currency, top frame id, top loader, merchant origin) of the last successful fill in this lock
        # whose card stated the total and whose page check matched it (record_approved_total); None: no press is
        # covered by the card. r4 C8: ``UNBOUND`` when such a card covered Pay but its total could not be bound.
        self.approved: Any = None
        # r4 C7: a press checked against ``approved`` (the total read, or a bank step after such a press) started
        # since that approval was bound. Reset with ``approved`` by every claim; owner_go presses never set it.
        self.admitted = False
        # build G1 (owner 2026-11-19: detect the payment, never limit presses): order evidence was found after a
        # Pay-like press of this approval (``browser_payment_detect``): the approval is spent, and every later
        # Pay-like press in this record is refused; ``paid_by``: which press and what evidence. A new fill starts a
        # new record (a new approval).
        self.paid = False
        self.paid_by: Optional[dict] = None
        # build C r4: (top frame id, loader, merchant origin) of the checkout this record's claim was for, kept apart
        # from ``approved`` (the payment detection reads its site); None: not known
        self.merchant: Optional[tuple] = None
        # build F r2/G1: the latest Pay-like press on the merchant's top document (the first one wherever it was):
        # its redacted caption and (frame id, loader, backendNodeId or None, origin + path), for payment_done's and
        # the detection's identity veto; ``pay_at``: (top frame id, monotonic) of the latest Pay-like press (the
        # in-flight guard)
        self.pay_caption = ""
        self.pay_node: Optional[tuple] = None
        self.pay_at: Optional[tuple] = None
        # build G2: ``{"doc": (frame id, loader), "strong": bool or None}`` read right before the latest Pay-like press
        # (the strong order wording the document showed before it; the detection counts only what is new)
        self.pay_base: Optional[dict] = None
        self.pay_tabs: Optional[frozenset] = None  # G3: the tabs this approval's Pay-like presses opened


# supervisor -> its current lock (set by a claim that starts one, dropped when an unlock is proven)
_GENS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_RECORD_SEQ = 0  # build C2: lock record serials (log only)
_CLICK_CLOSING = ("payment_close is closing the payment tab on this browser, so nothing was pressed. Wait for its "
                  "result; if the payment goes on, call payment_controls again afterwards.")


def pressing() -> bool:
    """Whether a payment_click of this process is still running (its press may be on the wire)."""
    with _LOCK:
        return _PRESSING_N > 0


def admit_close(supervisor: Any) -> tuple[bool, Optional[bool]]:
    """payment_close's admission: ``(False, None)`` while any payment_click of this process runs (nothing may be
    closed). Otherwise the close is marked running, so no click starts until ``end_close``, and the second value
    is ``pressed_in_lock``: True or False only when this process held this supervisor's lock since it started
    (and saw it whole); None when that is unknown (a replaced supervisor, a restart, an unlock read in between)."""
    global _CLOSING_N
    with _LOCK:
        if _PRESSING_N > 0:
            return False, None
        _CLOSING_N += 1
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        return True, (gen.pressed if gen is not None and gen.known else None)


def pay_press(supervisor: Any) -> Optional[dict]:
    """build F r2/G1: the latest Pay-like press in this supervisor's current lock (``_book_press``): ``caption`` (as
    listed: redacted), ``frame`` and ``loader`` of its document, ``node`` (its backendNodeId, None: unread) and ``key``
    (the document's origin + path); None when no Pay-like press of this process is recorded in it."""
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        if gen is None or gen.pay_node is None:
            return None
        frame, loader, node, key = gen.pay_node
        return {"caption": gen.pay_caption, "frame": frame, "loader": loader, "node": node, "key": key}


def presses_in_lock(supervisor: Any) -> Optional[int]:
    """How many payment_click presses this process started in this supervisor's current lock (a press that may have
    landed counts), so Sera and the owner can see a second Pay before it happens. None when unknown, on the same
    terms as ``pressed_in_lock`` (a restart, a replaced supervisor, an unlock read in between)."""
    with _LOCK:
        gen = _GENS.get(supervisor) if supervisor in _HELD else None
        return gen.presses if gen is not None and gen.known else None


def end_close() -> None:
    """Once per admitted close, on every exit."""
    global _CLOSING_N
    with _LOCK:
        _CLOSING_N = max(0, _CLOSING_N - 1)


def closing() -> bool:
    with _LOCK:
        return _CLOSING_N > 0


def payment_click(supervisor: Any, ref: str, owner_go: bool = False) -> dict:
    """``browser_vault action=payment_click ref=…``: one trusted click on a control of the latest listing.
    Refs are single-use: the ref is taken out of the listing for the click and stays dead once a press
    started (successful or uncertain); a second press needs a fresh payment_controls.

    B1: a lock with an approved total (``record_approved_total``) admits a press only through ``_pay_gate``; a lock
    without one refuses ``payment_go_required`` unless ``owner_go`` (Sera's word that the owner gave his go in chat)."""
    global _PRESSING_N
    gen = None
    gate = _Gate(None, False, owner_go is True, 0, None)
    with _LOCK:  # dlz (Codex r3/r4 P1): one step admits the click against a payment_close running anywhere
        if _CLOSING_N > 0:  # the ref stays in its listing
            return _refusal("payment_close_busy", _CLICK_CLOSING)
        if _PRESSING.get(supervisor, 0) > 0:  # build C r2 (P2, r3 Q3): one click at a time per browser, its settle
            return _refusal(CLICK_BUSY, _CLICK_BUSY_TEXT)  # included; the ref stays
        issued = _ISSUED.get(supervisor)
        spec = issued.pop(ref, None) if isinstance(ref, str) and _REF_RE.fullmatch(ref) and issued else None
        if spec is not None:  # dlz (Opus r2): payment_close does not close the tab under a press still running
            # the lock this press belongs to; build G1: an owner_go press meets the same payment detection
            gen = _GENS.get(supervisor) if supervisor in _HELD else None
            _PRESSING_N += 1
            _PRESSING[supervisor] = _PRESSING.get(supervisor, 0) + 1
            # r4 C2: the approval and the claim generation it belongs to, re-checked right before the press
            gate = _Gate(gen.approved if gen is not None else None, gen is not None and gen.admitted,
                         owner_go is True, _EPOCHS.get(supervisor, 0), gen)
    if spec is None:
        return _refusal("payment_control_unknown", "ref must be one returned by the latest payment_controls and "
                                                   "not used for a press yet; call payment_controls again.")
    progress: list = []
    try:
        out = _with_dialog(supervisor, _run(
            supervisor, lambda deadline: _click(supervisor, spec, progress, deadline, gate), progress))
    finally:
        with _LOCK:  # build C r2 (P2): the press itself was booked at its dispatch (_book_press)
            if progress and _BOOKED not in progress and gen is not None and _GENS.get(supervisor) is gen:
                gen.pressed = True  # a press marked outside _book_press still counts (never admits, never pays)
                gen.presses += 1
            _PRESSING_N = max(0, _PRESSING_N - 1)
            _PRESSING[supervisor] = max(0, _PRESSING.get(supervisor, 0) - 1)
    if out.get("success"):
        out["clicked"] = ref
    elif not progress:  # nothing was pressed: the ref stays usable, if its listing is still the latest
        with _LOCK:
            if _ISSUED.get(supervisor) is issued:
                issued[ref] = spec
    return out
