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

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable
from itertools import islice
from typing import Any

from agent.vault_payment_policy import PaymentDestinationError

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

# dlz (11-15, D-0094 "b"): the visibility rule an unreviewed fill's frames were judged by (reviewed fills: no key),
# and (r8) the rule that chose them: the strict one, or the shown one when the strict one found no complete card form.
_EXPOSURES = frozenset({"shown", "strict"})
_ADMISSIONS = frozenset({"strict", "shown_fallback"})
STOPPED_SCHEMA = {"stage": _STAGES, "reason": _REASONS, "exposure": _EXPOSURES, "admission": _ADMISSIONS}


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


def admission_strict(i: Any) -> bool:
    """dlz (11-15 r8, Opus P2-1): whether an unreviewed inspection's frames were chosen, and its new frames are judged,
    by the strict exposure rule: anything but a recorded ``shown_fallback`` is (the strict rule is the safe default)."""
    return bool(getattr(i, "unreviewed_origins", ())) and (
        strict_exposure(i) or getattr(i, "admission", "strict") != "shown_fallback")


def admission_rule(i: Any) -> str:
    """``strict`` or ``shown_fallback`` for an unreviewed fill's stop record; "" for a reviewed one."""
    if not getattr(i, "unreviewed_origins", ()):
        return ""
    return "strict" if admission_strict(i) else "shown_fallback"


def exposure_rule(i: Any) -> str:
    """The visibility rule an unreviewed fill ran under, for its stop record: ``strict`` or ``shown``; "" for a reviewed
    fill (or anything that is not an inspection)."""
    if not getattr(i, "unreviewed_origins", ()):
        return ""
    return "strict" if strict_exposure(i) else "shown"


def stopped_record(stage: str, stale: Any, exposure: str = "", admission: str = "") -> dict:
    """``{stage, reason}`` with both from the fixed vocabulary (``unknown`` for anything else), and ``exposure``
    (``shown`` / ``strict``) and ``admission`` (``strict`` / ``shown_fallback``) when the fill was an unreviewed one."""
    reason = stale if type(stale) is str and stale in _REASONS else "unknown"
    return {"stage": stage if stage in _STAGES else "in_document", "reason": reason,
            **({"exposure": exposure} if exposure in _EXPOSURES else {}),
            **({"admission": admission} if admission in _ADMISSIONS else {})}


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
    return stopped_record(stage, reason, exposure_rule(inspection), admission_rule(inspection))


async def strict_then_shown(run: Callable[[str], Awaitable], budget: float | None, reserve: float) -> Any:
    """r9 (Codex r8 P2): an unreviewed inspection's two passes under one deadline. ``run(admission)`` is one full
    pass. The strict pass goes first; only its typed ``payment_fields_unsupported`` starts the shown-rule fallback, and
    only when ``budget`` seconds (the caller's own wrapper, counted from now; None: unbounded) still leave more than
    ``reserve`` (the bound the cleanup of a cancelled pass needs, as ``discard_payment``'s) after the strict pass. A
    fallback that cannot start, or that outlasts what is left, ends in the strict pass's refusal, never in the
    wrapper's bare timeout. Any other fallback outcome (a result, its own typed refusal) stands."""
    until = None if budget is None else time.monotonic() + budget - reserve
    try:
        return await run("strict")
    except PaymentDestinationError as exc:
        if exc.error_type != "payment_fields_unsupported":
            raise
        refusal = exc
    left = None if until is None else until - time.monotonic()
    if left is not None and left <= 0:
        raise refusal
    try:
        async with asyncio.timeout(left) as scope:
            return await run("shown_fallback")
    except TimeoutError:
        if not scope.expired():
            raise
    raise refusal


def track_cleanup(sup: Any, task: asyncio.Future) -> asyncio.Future:
    """r10: keep a release task referenced on its supervisor (``_payment_cleanups``) until it ends: the loop holds
    tasks only weakly. Supervisor shutdown cancels it with every other pending task (``_thread_main``)."""
    tasks = sup.__dict__.setdefault("_payment_cleanups", set())
    tasks.add(task)
    task.add_done_callback(lambda t: (tasks.discard(t), t.cancelled() or t.exception()))
    return task


async def await_cleanup(sup: Any, cleanup: asyncio.Future, end: float | None) -> None:
    """r10 (Codex, Kimi r9 P2): wait for a refused or cancelled pass's release task until ``end`` (monotonic; None:
    to its end). Its releases run one after another, each up to the CDP timeout, so a few stalled ones outlast any
    reserve; past ``end`` it goes on in the background (tracked) and the pass's own exception (a typed refusal, the
    fallback deadline's cancellation) reaches the caller before the wrapper does. A cancellation of the waiting pass
    propagates; the task goes on the same way."""
    track_cleanup(sup, cleanup)
    await asyncio.wait((cleanup,), timeout=None if end is None else max(0.0, end - time.monotonic()))


async def release_object(sup: Any, sid: str, obj: str) -> None:
    """r10: one pinned object's release from a ``finally``: awaited, except while the pass is being cancelled (the
    fallback deadline, the wrapper), when it goes on in the background (tracked) instead of holding that up."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        track_cleanup(sup, asyncio.ensure_future(sup._release_payment_object(sid, obj)))
        return
    await sup._release_payment_object(sid, obj)
