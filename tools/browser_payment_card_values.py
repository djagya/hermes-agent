"""dlz: the saved card's values a payment window keeps in memory, and when they go.

Two places hold them, both filled by the card fill before any field is written:

* the profile-wide payment window (:func:`agent.redact_vault.register_card_window`): the number and CVC by
  length (every model-facing output masks every digit run of their lengths, so nothing marks which run
  was the card) and the holder name, keyed by the slot (by the supervisor's own identity without a slot
  route, :func:`window_key`). A confirmed payment lock release (:func:`release_payment_window`: control
  proves the slot unlocked after this process saw it locked, which ``payment_done``'s ``lock_released``, a
  recovery close of the payment tab and any later status read all go through) narrows it to browser tool
  results (the page may still show card text); it closes when the slot's exposure ends
  (:func:`release_slot`: a wipe, a human clear or an operator clear of its mark). Every release is fenced
  by the window generation it observed first: a claim (:func:`note_claim`) or an open since then wins;
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


def slot_of(supervisor: Any) -> str:
    """The managed slot id of ``supervisor``'s browser_exec route; ``""`` when it has none."""
    if supervisor is None:
        return ""
    try:
        from tools.browser_use_cli import vault_route

        return str((vault_route(getattr(supervisor, "task_id", None)) or {}).get("slot_id") or "")
    except Exception:  # health: allow BLE001 -- no route to name a slot
        return ""


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


def window_key(supervisor: Any) -> str:
    """The payment window ``supervisor`` opens: its slot id, or (no slot route) its own identity, never ``""``,
    so one slot-less supervisor's release never closes another's window."""
    slot = slot_of(supervisor)
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
    """The generations of ``supervisor``'s windows, read before the status read that may release them."""
    from agent.redact import card_window_generations

    with _LOCK:
        try:
            keys = set(_WINDOWS.get(supervisor, ()))
        except TypeError:
            keys = set()
    keys.add(window_key(supervisor))
    gens = card_window_generations()
    return {key: gens.get(key, 0) for key in keys}


def open_payment_window(secret: dict, supervisor: Any) -> None:
    """Register the card for every model-facing output of this profile until the release is confirmed.
    Raises when it cannot (the fill must then stop)."""
    from agent.redact import register_card_window

    key = window_key(supervisor)
    register_card_window(key, digits=(str(secret.get("card_number") or ""), str(secret.get("cvc") or "")),
                         words=(str(secret.get("cardholder_name") or ""),))
    with _LOCK:
        try:
            _WINDOWS.setdefault(supervisor, set()).add(key)
        except TypeError:  # unscoped: closed only by its slot's release (or never: masking stays)
            pass


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
