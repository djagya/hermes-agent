"""Where a refused card-fill re-inspection stopped, and how its window was spent (dlz, 11-14).

A card fill that stops on "the form is not there" used to say only that: FlixBus / Payrails stopped like that twice on
11-14 and the result could not tell a page that changed on its own clock from one that changed when the fill touched
it. ``fill_reinspect.stopped`` (``{stage, reason}``) and ``fill_reinspect.window`` (polls, milliseconds, how often each
refusal code came, how long after the inspection the window ended) say so.

Nothing here reads the page or calls the browser. Both records are computed from what the fill path holds anyway
(``PaymentInspection.stale``: the check that returned false; the re-inspection loop's own refusal codes and clock) and
reduced to a fixed schema (``_clean``: an allow-list of keys with enumerated codes or counts, never page text, an
origin, a label or a value) before they go into a result.
"""

from __future__ import annotations

import functools
import time
from itertools import islice
from typing import Any

_STAGES = frozenset({"before_write", "after_focus", "between_documents", "in_document", "after_approval"})
# ``PaymentInspection.stale``: the check under way when a freshness pass refused (``_error``: it raised).
_CHECKS = frozenset({"inactive", "tree", "frames", "frames_top", "frames_card", "frames_new", "document", "absence",
                     "shown", "owner_hit", "owner_shown", "expired", "cart", "policy", "scope_chain", "scope_unpinned", "scope_tree",
                     "scope_frame", "scope_owner", "unknown"})
_REASONS = _CHECKS | {c + "_error" for c in _CHECKS}
# Exception classes a re-inspection poll may meet, by name; any other is ``other``.
_ERRORS = frozenset({"timeouterror", "cancellederror", "runtimeerror", "connectionerror", "connectionclosed",
                     "connectionclosederror", "oserror", "keyerror", "typeerror", "valueerror", "attributeerror",
                     "indexerror", "other"})

# dlz (11-15, D-0094 "b"): the visibility rule an unreviewed fill's frames were judged by (reviewed fills: no key).
_EXPOSURES = frozenset({"shown", "strict"})
STOPPED_SCHEMA = {"stage": _STAGES, "reason": _REASONS, "exposure": _EXPOSURES}


@functools.cache
def _codes() -> frozenset:
    """The re-inspection's own refusal codes (``tools.browser_supervisor_payment_auto``), and ``matched``."""
    from tools import browser_supervisor_payment_auto as spa

    return spa._REINSPECT_FINAL | spa._REINSPECT_RETRIED | {"matched"}


@functools.cache
def window_schema() -> dict:
    return {"polls": int, "ms": int, "inspected_ms_ago": int, "codes": ("map", _codes(), int),
            "errors": ("map", _ERRORS, int)}


def _clean(value: Any, schema: Any) -> Any:
    """``value`` reduced to ``schema``: a record keeps only its own keys, a map only enumerated keys; ``int`` is a count,
    a frozenset the codes a string may be. Anything else is dropped (None when nothing is left)."""
    if schema is int:
        return value if type(value) is int and 0 <= value < 10**7 else None
    if isinstance(schema, frozenset):
        return value if type(value) is str and value in schema else None
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            return None
        out = {k: c for k, s in schema.items() if (c := _clean(value.get(k), s)) is not None}
        return out or None
    _kind, bound, inner = schema  # ("map", keys, value)
    if not isinstance(value, dict):
        return None
    out = {k: c for k, v in islice(value.items(), 2 * len(bound))
           if type(k) is str and k in bound and (c := _clean(v, inner)) is not None}
    return out or None


class ReinspectLog:
    """How one re-inspection window was spent: polls, elapsed milliseconds, and how often each refusal code (and each
    exception type, by its class name from a fixed list) came. Codes are the re-inspection's own fixed ones."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.polls = 0
        self.codes: dict = {}
        self.errors: dict = {}

    def note(self, code: str) -> None:
        self.polls += 1
        self.codes[code or "matched"] = self.codes.get(code or "matched", 0) + 1

    def error(self, exc: BaseException) -> None:
        name = type(exc).__name__.lower()
        name = name if name in _ERRORS else "other"
        self.errors[name] = self.errors.get(name, 0) + 1

    def record(self, inspected_ms_ago: int) -> dict | None:
        """The window as a sanitized record. ``inspected_ms_ago``: how long after the inspection the approval card was
        built from this window ended (the approval's window is fixed, so its end dates it)."""
        return _clean({"polls": self.polls, "ms": int((time.monotonic() - self.started) * 1000),
                       "inspected_ms_ago": inspected_ms_ago, "codes": self.codes,
                       **({"errors": self.errors} if self.errors else {})}, window_schema())


def clean_window(value: Any) -> dict | None:
    return _clean(value, window_schema())


def strict_exposure(i: Any) -> bool:
    """dlz (11-15, D-0094 "b"): whether a fill's frames are judged with the strict exposure rule: an unreviewed fill with
    vault.payment_consent_strict_exposure on at its discovery. A reviewed fill never is (its rules are unchanged)."""
    return bool(getattr(i, "unreviewed_origins", ())) and getattr(i, "strict_exposure", False) is True


def exposure_rule(i: Any) -> str:
    """The visibility rule an unreviewed fill ran under, for its stop record: ``strict`` or ``shown``; "" for a reviewed
    fill (or anything that is not an inspection)."""
    if not getattr(i, "unreviewed_origins", ()):
        return ""
    return "strict" if strict_exposure(i) else "shown"


def stopped_record(stage: str, stale: Any, exposure: str = "") -> dict:
    """``{stage, reason}`` with both from the fixed vocabulary (``unknown`` for anything else), and ``exposure``
    (``shown`` / ``strict``) when the fill was an unreviewed one."""
    reason = stale if type(stale) is str and stale in _REASONS else "unknown"
    return {"stage": stage if stage in _STAGES else "in_document", "reason": reason,
            **({"exposure": exposure} if exposure in _EXPOSURES else {})}


def _stopped(result: dict, inspection: Any, st: dict) -> dict | None:
    """Where a generation of an armed commit stopped on the PAGE, for ``fill_reinspect.stopped``: ``before_write``
    (the page check at its start), ``between_documents`` (the checks before a later document, named by
    ``_commit_step``), ``after_focus`` (the check after a field's focus, before its setter: ``stale`` starts with
    ``focus_``) or ``in_document`` (the document's own closure refused, with no check of the approved page on record:
    reason ``unknown``). ``reason`` is the part of the approved page that did not hold (``PaymentInspection.stale``).
    None for a stop a document's own checks or the retention check made: ``fill_check`` / ``field_retention`` say that."""
    stage = st.pop("stage", "")
    if isinstance(result.get("fill_check"), dict) or result.get("error_type") == "payment_fields_not_retained":
        return None
    reason = str(getattr(inspection, "stale", "") or "unknown")
    if not stage:
        if reason.startswith("focus_"):
            stage, reason = "after_focus", reason[6:]
        else:
            stage, reason = "in_document", "unknown"
    return stopped_record(stage, reason, exposure_rule(inspection))
