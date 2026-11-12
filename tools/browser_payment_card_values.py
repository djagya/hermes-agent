"""dlz: the saved card's values a payment window keeps in memory, and when they go.

Two places hold them, both filled by the card fill before any field is written:

* the profile-wide payment window (:func:`agent.redact_vault.register_card_window`): the number and CVC by
  length (every model-facing output masks every digit run of their lengths, so nothing marks which run
  was the card) and the holder name (browser and log output only), keyed by the slot (by the supervisor's own identity without a slot
  route, :func:`window_key`). A confirmed payment lock release (:func:`release_payment_window`: control
  proves the slot unlocked, which ``payment_done``'s ``lock_released`` and any later status read (after a recovery close of the payment
  tab too) all go through, by whichever supervisor is attached to the slot,
  :func:`slot_bound`; and :func:`release_unlocked_slot`, control's own view of the slot, when no supervisor
  reads it) narrows it to browser tool results (the page may still show card text); it closes when the
  slot's exposure ends (:func:`release_slot`: a wipe, a human clear or an operator clear of its mark).
  Every release is fenced by the window generation it observed first: a claim (:func:`note_claim`) or an
  open since then wins. A fill that stops before its first write takes its open back
  (:func:`withdraw_payment_window`, by the key and identity the open returned; sealed by
  :func:`seal_payment_window` when the commit starts): nothing of the card reached the page;
* the dialog values (number, CVC, holder, expiry forms): per supervisor AND per slot, so a fresh supervisor
  for the same slot still scrubs them from dialog text while the slot's mark keeps it exposed. Dropped,
  every supervisor's on the slot included, only when the slot's exposure ends; they are used for dialog and
  caption text only, where every digit run is masked first.
"""

from __future__ import annotations

import itertools
import threading
import weakref
from typing import Any

_LOCK = threading.Lock()
_BY_SUPERVISOR: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # supervisor → dialog values
_BY_SLOT: dict = {}  # (profile home, slot id) → dialog values
_WINDOWS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # supervisor → payment window keys it opened
_SUPERVISOR_SLOTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # supervisor → slot of its dialog values
_SUPERVISOR_KEYS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # slot-less supervisor → its window key
_KEY_COUNTER = itertools.count(1)
SUPERVISOR_KEY_PREFIX = "supervisor:"
MAX_VALUES = 64


def _scope() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home())


def route_of(supervisor: Any) -> dict | None:
    """One snapshot of ``supervisor``'s browser_exec route (a copy), ``None`` when there is no route to read.
    A caller that needs the slot and the bound check of the same moment derives both from one snapshot."""
    if supervisor is None:
        return None
    try:
        from tools.browser_use_cli import vault_route

        return dict(vault_route(getattr(supervisor, "task_id", None)) or {})
    except Exception:  # health: allow BLE001 -- no route to read
        return None


def slot_of(supervisor: Any, route: dict | None = None) -> str:
    """The managed slot id of ``supervisor``'s browser_exec route (``route``: a snapshot already taken);
    ``""`` when it has none."""
    if route is None:
        route = route_of(supervisor)
    return str((route or {}).get("slot_id") or "")


def _dialog_values(secret: dict) -> list:
    values = [str(secret.get("card_number") or ""), str(secret.get("cvc") or "")]
    values.append(str(secret.get("cardholder_name") or "").strip())  # every nonempty holder, "Li" included
    month, year = str(secret.get("exp_month") or ""), str(secret.get("exp_year") or "")
    if month.isdigit() and year.isdigit() and len(year) == 4:
        mm, yy = month.zfill(2), year[2:]
        for m in dict.fromkeys((mm, str(int(mm)))):
            for y in (yy, year):
                values += [f"{m}/{y}", f"{m} / {y}", f"{m}-{y}"]
    return [v for v in dict.fromkeys(values) if v]


def register_dialog_card_values(secret: dict, supervisor: Any) -> None:
    """The card fill's validated card, for the dialog and caption text of ``supervisor`` and its slot."""
    values, slot = _dialog_values(secret), slot_of(supervisor)
    with _LOCK:
        targets = []
        try:
            targets.append(_BY_SUPERVISOR.setdefault(supervisor, []))
            _SUPERVISOR_SLOTS[supervisor] = slot
        except TypeError:  # nothing to scope by: the slot (if any) and the digit mask still cover it
            pass
        if slot:
            targets.append(_BY_SLOT.setdefault((_scope(), slot), []))
        for held in targets:
            held.extend(v for v in values if v not in held)
            del held[:-MAX_VALUES]


def has_dialog_card_values(supervisor: Any) -> bool:
    """Whether a card fill registered dialog values for ``supervisor`` itself (its slot's own card)."""
    with _LOCK:
        try:
            return supervisor in _BY_SUPERVISOR
        except TypeError:
            return False


def dialog_card_values(supervisor: Any) -> list:
    """The card values a dialog seen through ``supervisor`` is scrubbed of: its own and its slot's; without
    a slot route, every slot's of this profile (more scrubbing, never less)."""
    slot = slot_of(supervisor)
    with _LOCK:
        try:
            own = list(_BY_SUPERVISOR.get(supervisor, ())) if supervisor is not None else []
        except TypeError:
            own = []
        scope = _scope()
        if slot:
            extra = list(_BY_SLOT.get((scope, slot), ()))
        else:
            extra = [v for (home, _slot), held in _BY_SLOT.items() if home == scope for v in held]
    return list(dict.fromkeys(own + extra))


def window_key(supervisor: Any, route: dict | None = None) -> str:
    """The payment window ``supervisor`` opens: its slot id, or (no slot route) its own identity, never ``""``,
    so one slot-less supervisor's release never closes another's window. ``route``: a snapshot already taken."""
    slot = slot_of(supervisor, route)
    if slot:
        return slot
    with _LOCK:
        try:
            key = _SUPERVISOR_KEYS.get(supervisor)
            if key is None:
                _SUPERVISOR_KEYS[supervisor] = key = f"{SUPERVISOR_KEY_PREFIX}{next(_KEY_COUNTER)}"
            return key
        except TypeError:
            return f"{SUPERVISOR_KEY_PREFIX}id-{id(supervisor)}"


def note_claim(supervisor: Any) -> None:
    """A payment claim on ``supervisor``'s slot started: a release that read its status before is stale."""
    from agent.redact import bump_card_window

    bump_card_window(window_key(supervisor))


def observe_windows(supervisor: Any) -> dict:
    """The generations of ``supervisor``'s windows, read before the status read that may release them: those
    it opened, and its route's slot only while its socket is the one that route names (:func:`slot_bound`).
    An orphaned supervisor whose route moved to another slot reads its own browser's status, which says
    nothing about the slot the route names now."""
    from agent.redact import card_window_generations

    with _LOCK:
        try:
            keys = set(_WINDOWS.get(supervisor, ()))
        except TypeError:
            keys = set()
    if slot_bound(supervisor):
        keys.add(window_key(supervisor))
    gens = card_window_generations()
    return {key: gens.get(key, 0) for key in keys}


def slot_bound(supervisor: Any, route: dict | None = None) -> bool:
    """Whether ``supervisor``'s socket is the one its task's route was attached with, so the route's slot id
    names the browser its status reads answer for. A supervisor without a slot route has only its own
    window (:func:`window_key`): bound. A route whose attach failed, or one a newer exec moved to another
    browser while this socket stayed, is not: its status says nothing about the slot the route names.
    ``route``: a snapshot already taken (:func:`route_of`); without one the route is read now."""
    if route is None:
        route = route_of(supervisor)
    if route is None:  # no route to compare: only the supervisor's own window
        return True
    if not route.get("slot_id"):
        return True
    url = str(route.get("supervisor_url") or "")
    return bool(url) and url == str(getattr(supervisor, "cdp_url", "") or "")


class PaymentWindowRefused(RuntimeError):
    """The window cannot be opened for this supervisor: the fill stops, nothing is written."""


def open_payment_window(secret: dict, supervisor: Any) -> tuple:
    """Register the card for every model-facing output of this profile until the release is confirmed.
    Raises when it cannot (the fill must then stop): on a managed browser also when the supervisor's socket
    is not the one its route names (:func:`slot_bound`), because the key would then name a slot this socket
    does not drive. Returns ``(window key, open identity, supervisor, recorded)`` of this open, for
    :func:`withdraw_payment_window` and :func:`seal_payment_window`: the key is fixed here, so a route that
    moves to another slot before the fill ends never makes it touch that slot's window; ``recorded`` says
    whether this open added the key to the supervisor's own windows. The bind check and the key come from ONE
    snapshot of the route: a route that moves between two reads cannot key the open on a slot the check never
    saw. The refusal is its own fail-closed guard (nothing is written, no window registered)."""
    from agent.redact import register_card_window
    from tools.browser_payment_surface import _managed

    route = route_of(supervisor)
    if _managed() and not slot_bound(supervisor, route):
        raise PaymentWindowRefused("supervisor is not bound to its route's slot")
    key = window_key(supervisor, route)
    opened = register_card_window(
        key, digits=(str(secret.get("card_number") or ""), str(secret.get("cvc") or "")),
        words=(str(secret.get("cardholder_name") or ""),))
    recorded = False
    with _LOCK:
        try:
            keys = _WINDOWS.setdefault(supervisor, set())
            recorded = key not in keys
            keys.add(key)
        except TypeError:  # unscoped: closed only by its slot's release (or never: masking stays)
            pass
    return key, opened, supervisor, recorded


def withdraw_payment_window(key: str, opened: int, supervisor: Any = None, recorded: bool = False) -> bool:
    """The fill whose open returned ``(key, opened, ...)`` stopped before it wrote any card value: that
    window goes back to what it was before the open (an earlier payment's window stays). Refused, and the
    window kept, when a claim or another fill touched it since, or once that open was sealed. Taken back or
    not, the supervisor stops owning ``key`` unless it owned it before this open (``recorded`` false): an
    unwritten open leaves no association for a later status read to release a slot by, even when another
    fill claimed or opened that slot meanwhile and the window stays for it (its own supervisor owns it)."""
    from agent.redact import withdraw_card_window

    taken = withdraw_card_window(key, opened)
    if recorded and supervisor is not None:
        with _LOCK:
            try:
                _WINDOWS.get(supervisor, set()).discard(key)
            except TypeError:
                pass
    return taken


def seal_payment_window(key: str, opened: int, *_ignored: Any) -> None:
    """The fill whose open returned ``(key, opened, ...)`` starts its commit: from here a card value may be in
    the page, so no withdraw can take that open back; only control's unlock or a mark clear ends it. Raises
    when that window is gone (the fill must stop before it writes)."""
    from agent.redact import seal_card_window

    seal_card_window(key, opened)


def release_payment_window(supervisor: Any, observed: dict) -> None:
    """Control proved ``supervisor``'s slot unlocked after a lock this process saw: each window it observed
    (``observe_windows``) and no claim touched since keeps masking browser tool results only (the page may
    still show card text) until its slot's mark is cleared; terminal and every other output stop masking."""
    from agent.redact import release_card_window

    for key, generation in observed.items():
        if release_card_window(key, generation, keep_for_browser=True):
            with _LOCK:
                try:
                    _WINDOWS.get(supervisor, set()).discard(key)
                except TypeError:
                    pass


def unreleased_slots() -> list:
    """The slots whose payment window still masks every output of this profile (no confirmed release yet)."""
    from agent.redact import card_window_keys

    return [k for k in card_window_keys(unreleased=True) if k and not k.startswith(SUPERVISOR_KEY_PREFIX)]


def release_unlocked_slot(slot_id: str, observed: dict) -> bool:
    """Control's own view of ``slot_id`` proved its payment lock gone (the tab was closed by someone else, a
    human clear): its window narrows to browser tool results, as after a supervisor's confirmed release,
    unless a claim or a card fill touched it since ``observed`` (the generations read before control was
    asked). Whether it narrowed."""
    from agent.redact import release_card_window

    slot = str(slot_id or "")
    return bool(slot) and slot in observed and release_card_window(slot, observed[slot], keep_for_browser=True)


def release_slot(slot_id: str, observed: dict | None = None) -> None:
    """The slot's payment exposure ended (its mark was cleared): its window and dialog values go, those of
    every supervisor on it included. ``""`` (a profile-wide mark) is every slot-less supervisor's window.
    ``observed`` (window generations read before the marks were) fences it: a window claimed or opened since
    stays, and so do its dialog values."""
    from agent.redact import card_window_keys, release_card_window

    slot = str(slot_id or "")
    keys = [slot] if slot else [k for k in card_window_keys() if k.startswith(SUPERVISOR_KEY_PREFIX)]
    released = {k for k in keys if release_card_window(k, None if observed is None else observed.get(k, 0))}
    if slot and slot not in released and observed is not None:
        return
    with _LOCK:
        if slot:
            _BY_SLOT.pop((_scope(), slot), None)
        for sup in [s for s, held_slot in list(_SUPERVISOR_SLOTS.items()) if held_slot == slot]:
            if not slot and observed is not None and _SUPERVISOR_KEYS.get(sup) not in released:
                continue  # its window was claimed again since: its values stay with it
            _BY_SUPERVISOR.pop(sup, None)
            _SUPERVISOR_SLOTS.pop(sup, None)
