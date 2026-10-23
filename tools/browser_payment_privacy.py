"""Capture guard after native card ingress.

A card fill marks the surface it wrote into. In managed browser-control mode
that surface is the leased slot, an isolated Chrome container named by
control; elsewhere it is the whole profile. ``browser_exec`` checks the slot
its own lease resolved to (:func:`capture_blocked_for_surface`), so a
research slot stays capturable while the payment slot does not. Tools that
cannot name an authoritative slot (built-in browser tools, raw CDP, desktop
capture) still refuse while any mark exists (:func:`payment_session_sensitive`).

Marks never clear on navigation, lease release, a new browser generation or a
gateway restart: a persistent profile, and a temporary slot restarted without
a wipe, can restore the payment tab. They persist in
``$HERMES_HOME/browser/payment-capture-guard.json``. One mark clears by itself:
a mark on a *temporary* slot that recorded control's wipe counter at fill time,
once control reports that slot with a higher counter and a different browser
generation (its profile was wiped after the fill; :func:`_prune_wiped`). Any
missing, malformed or unreachable answer keeps the mark. Every other mark is
cleared only by an operator (``python -m tools.browser_payment_privacy clear``)
after confirming the slot's payment tab is gone. Each clear, automatic or
operator, is appended to ``payment-capture-guard.log`` beside the file. An
unreadable mark file refuses every capture.

This prevents accidental capture through standard browser tools; arbitrary
terminal/code execution is not a containment boundary (see SECURITY.md).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

# The file is the only state, so an operator clear reaches a running gateway.
_LOCK = threading.Lock()

PROFILE_SCOPE = "profile"
SLOT_SCOPE = "slot"


def _state_path() -> Path:
    return Path(get_hermes_home()) / "browser" / "payment-capture-guard.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_marks(path: Path) -> list[dict[str, Any]] | None:
    """Marks on disk; [] when there is no file, None when it cannot be trusted."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    marks = data.get("marks") if isinstance(data, dict) else None
    if not isinstance(marks, list) or not all(isinstance(m, dict) for m in marks):
        return None
    return marks


def _write_marks(path: Path, marks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".payment-capture-guard.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "marks": marks}, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _all_marks() -> list[dict[str, Any]] | None:
    """Marks for the current profile; None = fail closed."""
    return _read_marks(_state_path())


def mark_payment_session(task_id: str, surface: dict[str, str] | None = None) -> None:
    """Record card ingress before the write. Raises when the mark cannot be persisted.

    ``surface`` is ``{"slot_id", "browser_generation"}`` from the managed lease the
    fill used; anything less marks the whole profile.
    """
    slot_id = str((surface or {}).get("slot_id") or "").strip()
    generation = str((surface or {}).get("browser_generation") or "").strip()
    wipe_count = str((surface or {}).get("wipe_count") or "").strip()
    mark: dict[str, Any] = {"task_id": str(task_id or ""), "marked_at": _now()}
    if slot_id and generation:
        mark.update(scope=SLOT_SCOPE, slot_id=slot_id, browser_generation=generation)
        # Control's wipe counter for the slot when the card went in; only a
        # plain decimal is recorded, so a mark without it never auto-clears.
        if wipe_count.isdecimal() and wipe_count.isascii():
            mark["wipe_count"] = int(wipe_count)
    else:
        mark["scope"] = PROFILE_SCOPE
    with _LOCK:
        path = _state_path()
        disk = _read_marks(path)
        # An unreadable file already refuses every capture; leave it for the operator.
        if disk is None:
            raise OSError(f"payment capture guard state unreadable: {path}")
        _write_marks(path, disk + [mark])


def _int_field(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _wiped_since(mark: dict[str, Any], row: dict[str, Any]) -> bool:
    """Control attests this slot's temporary profile was wiped after ``mark``. Fail closed."""
    marked = _int_field(mark.get("wipe_count"))
    current = _int_field(row.get("wipe_count"))
    if marked is None or current is None or current <= marked:
        return False
    if row.get("error") or row.get("slot_id") != mark.get("slot_id") or row.get("kind") != "temporary":
        return False
    generation = str(row.get("browser_generation") or "")
    return generation != str(mark.get("browser_generation") or "")


def _prune_wiped(marks: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Drop slot marks whose temporary profile control reports wiped since the mark.

    Asks control once per marked slot, outside the lock; any failure keeps the
    mark. The file is re-read under the lock so a mark added meanwhile stays.
    """
    if not marks or not _managed():
        return marks
    candidates = {
        str(m.get("slot_id")) for m in marks
        if m.get("scope") == SLOT_SCOPE and m.get("slot_id") and _int_field(m.get("wipe_count")) is not None
    }
    if not candidates:
        return marks
    try:
        from tools.browser_control_route import slot_status
    except Exception:  # health: allow BLE001 -- no control client: keep every mark
        return marks
    rows: dict[str, dict[str, Any]] = {}
    for slot_id in sorted(candidates):
        try:
            row = slot_status(slot_id)
        except Exception:  # health: allow BLE001 -- control unreachable: keep the mark
            continue
        if isinstance(row, dict):
            rows[slot_id] = row
    if not rows:
        return marks

    def stale(mark: dict[str, Any]) -> bool:
        row = rows.get(str(mark.get("slot_id") or ""))
        return mark.get("scope") == SLOT_SCOPE and row is not None and _wiped_since(mark, row)

    with _LOCK:
        path = _state_path()
        disk = _read_marks(path)
        if disk is None:
            return None
        removed = [m for m in disk if stale(m)]
        if not removed:
            return disk
        kept = [m for m in disk if not stale(m)]
        _write_marks(path, kept)
        with path.with_suffix(".log").open("a", encoding="utf-8") as log:
            for slot_id in sorted({str(m.get("slot_id")) for m in removed}):
                log.write(json.dumps({
                    "cleared_at": _now(), "slot_id": slot_id, "confirmed_by": "auto:temporary-profile-wiped",
                    "wipe_count": rows[slot_id].get("wipe_count"),
                    "marks": [m for m in removed if str(m.get("slot_id")) == slot_id],
                }, sort_keys=True) + "\n")
        return kept


def payment_session_sensitive(task_id: str | None) -> bool:
    """Profile-wide answer for tools that cannot name an authoritative slot."""
    with _LOCK:
        marks = _all_marks()
    marks = _prune_wiped(marks)
    return marks is None or bool(marks)


def slot_has_mark(slot_id: str) -> bool:
    """Raw, local answer (no control lookup): any mark that covers ``slot_id``.
    An unreadable file, a profile-wide mark or a missing slot id answer True."""
    with _LOCK:
        marks = _all_marks()
    if marks is None or not str(slot_id or "").strip():
        return True
    return any(m.get("scope") != SLOT_SCOPE or m.get("slot_id") in (None, "", slot_id) for m in marks)


def capture_blocked_for_surface(slot_id: str, browser_generation: str) -> bool:
    """Whether a capture in the managed slot ``slot_id`` must be refused.

    The slot and generation must come from the lease control granted for this
    call. A missing identity, an unreadable mark file, a profile-wide mark or
    any mark on the same slot (whatever its generation) refuses.
    """
    if not str(slot_id or "").strip() or not str(browser_generation or "").strip():
        return payment_session_sensitive(None)
    with _LOCK:
        marks = _all_marks()
    marks = _prune_wiped(marks)
    if marks is None:
        return True
    return any(m.get("scope") != SLOT_SCOPE or m.get("slot_id") in (None, "", slot_id) for m in marks)


def clear_payment_surface(slot_id: str, *, confirmed_by: str) -> int:
    """Operator-only: drop the marks for ``slot_id`` (``profile`` = profile-wide marks).

    Returns the number of marks removed and appends an audit line to the log.
    """
    if not str(confirmed_by or "").strip():
        raise ValueError("confirmed_by is required")
    with _LOCK:
        path = _state_path()
        disk = _read_marks(path)
        if disk is None:
            raise OSError(f"payment capture guard state unreadable: {path}")

        def keep(mark: dict[str, Any]) -> bool:
            if slot_id == PROFILE_SCOPE:
                return mark.get("scope") == SLOT_SCOPE
            return mark.get("slot_id") != slot_id

        kept = [m for m in disk if keep(m)]
        removed = [m for m in disk if not keep(m)]
        _write_marks(path, kept)
        with path.with_suffix(".log").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"cleared_at": _now(), "slot_id": slot_id,
                                  "confirmed_by": confirmed_by, "marks": removed}, sort_keys=True) + "\n")
        return len(removed)


_CAPTURE_TOKENS = ("screenshot", "printtopdf", "screencast", "recording")


def _wants_capture(code: str) -> bool:
    lowered = (code or "").lower()
    return any(token in lowered for token in _CAPTURE_TOKENS)


def _managed() -> bool:
    from tools.browser_control_route import is_managed

    return is_managed()


def exec_capture_refusal(code: str, task_id: str | None) -> str | None:
    """browser_exec, before routing: the profile-wide refusal outside managed mode."""
    if _wants_capture(code) and not _managed() and payment_session_sensitive(task_id):
        return json.dumps(screenshot_refusal())
    return None


def exec_slot_capture_refusal(code: str, env: dict[str, str]) -> str | None:
    """browser_exec, after routing: refuse a capture on the managed slot this call was granted."""
    if not _wants_capture(code) or not _managed():
        return None
    slot_id = str(env.get("HERMES_BROWSER_SLOT_ID") or "")
    generation = str(env.get("HERMES_BROWSER_GENERATION") or "")
    return json.dumps(screenshot_refusal()) if capture_blocked_for_surface(slot_id, generation) else None


def screenshot_refusal() -> dict:
    return {
        "success": False,
        "error_type": "payment_screenshot_blocked",
        "error": "Screenshots/PDF/recordings are disabled on this browser after card fill. Use redacted "
        "text inspection, or capture with browser_exec in a research slot that never held the payment page.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.browser_payment_privacy",
        description="List or clear post-card-fill capture marks (operator only).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    clear = sub.add_parser("clear", help="only after confirming the slot's payment tab is gone")
    clear.add_argument("slot_id", help="managed slot id, or 'profile' for profile-wide marks")
    clear.add_argument("--confirmed-by", required=True)
    ns = parser.parse_args(argv)
    if ns.cmd == "list":
        marks = _read_marks(_state_path())
        if marks is None:
            print(f"unreadable: {_state_path()}", file=sys.stderr)
            return 1
        print(json.dumps(marks, indent=2, sort_keys=True))
        return 0
    removed = clear_payment_surface(ns.slot_id, confirmed_by=ns.confirmed_by)
    print(f"removed {removed} mark(s) for {ns.slot_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
