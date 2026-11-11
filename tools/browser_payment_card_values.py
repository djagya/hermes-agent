"""dlz: the saved card's values a payment window keeps in memory, and when they go.

Two places hold them, both filled by the card fill before any field is written:

* the profile-wide payment window (:func:`agent.redact_vault.register_card_window`): the number and CVC by
  length (every model-facing output masks every digit run of their lengths, so nothing marks which run
  was the card) and the holder name, keyed by the slot (``""`` without a slot route). It closes when the
  payment lock release is confirmed (:func:`release_payment_window`: control proves the slot unlocked
  after this process saw it locked, which ``payment_done``'s ``lock_released``, a recovery close of the
  payment tab and any later status read all go through) or the slot's exposure ends
  (:func:`release_slot`: a wipe, a human clear or an operator clear of its mark);
* the dialog values (number, CVC, holder, expiry forms): per supervisor AND per slot, so a fresh supervisor
  for the same slot still scrubs them from dialog text while the slot's mark keeps it exposed. Dropped only
  when the slot's exposure ends; they are used for dialog and caption text only, where every digit run is
  masked first.
"""

from __future__ import annotations

import threading
import weakref
from typing import Any

_LOCK = threading.Lock()
_BY_SUPERVISOR: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # supervisor → dialog values
_BY_SLOT: dict = {}  # (profile home, slot id) → dialog values
_WINDOWS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # supervisor → payment window keys it opened
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


def open_payment_window(secret: dict, supervisor: Any) -> None:
    """Register the card for every model-facing output of this profile until the release is confirmed.
    Raises when it cannot (the fill must then stop)."""
    from agent.redact import register_card_window

    key = slot_of(supervisor)
    register_card_window(key, digits=(str(secret.get("card_number") or ""), str(secret.get("cvc") or "")),
                         words=(str(secret.get("cardholder_name") or ""),))
    with _LOCK:
        try:
            _WINDOWS.setdefault(supervisor, set()).add(key)
        except TypeError:  # unscoped: closed only by its slot's release (or never: masking stays)
            pass


def release_payment_window(supervisor: Any) -> None:
    """Control proved ``supervisor``'s slot unlocked after a lock this process saw: close its windows."""
    from agent.redact import release_card_window

    with _LOCK:
        try:
            keys = set(_WINDOWS.pop(supervisor, ()))
        except TypeError:
            keys = set()
    slot = slot_of(supervisor)
    if slot:
        keys.add(slot)
    for key in keys:
        release_card_window(key)


def release_slot(slot_id: str) -> None:
    """The slot's payment exposure ended (its mark was cleared): its window and dialog values go."""
    from agent.redact import release_card_window

    release_card_window(str(slot_id or ""))
    with _LOCK:
        _BY_SLOT.pop((_scope(), str(slot_id or "")), None)
