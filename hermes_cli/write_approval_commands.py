#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands.

Both the interactive CLI (``cli.py``) and the gateway (``gateway/run.py``) call
into this module so the pending-review UX (list / approve / reject / diff /
mode) lives in one place. Each caller owns only its surface concerns:
formatting the returned text and, for the gateway, persisting config + evicting
the cached agent on a mode change.

Every public handler returns a plain text string suitable for both a terminal
and a chat message. Skill diffs are intentionally NOT inlined here — the
``diff`` handler returns the full diff for the CLI pager, but on a messaging
platform the gateway truncates it and points the user at the dashboard / file.
"""

from __future__ import annotations

import json
from typing import List, Optional

from tools import write_approval as wa


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    return f"{subsystem}.write_approval = {'on' if on else 'off'}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    lines = [f"Pending {subsystem} writes ({len(records)}):"]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        state = r.get("state", "pending")
        if state == "applying":
            tag += " [applying — recovery required]"
        if r.get("legacy_schema"):
            tag += " [legacy — restage required]"
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}")
    where = "/{s} approve <id>".format(s=subsystem)
    lines.append("")
    lines.append(f"Apply: {where}   Reject: /{subsystem} reject <id>")
    if subsystem == wa.SKILLS:
        lines.append("Review full diff: /skills diff <id>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------

def handle_pending_subcommand(
    subsystem: str,
    args: List[str],
    *,
    memory_store=None,
    set_mode_fn=None,
    expected_payload_sha256: Optional[str] = None,
) -> Optional[str]:
    """Dispatch a /memory or /skills subcommand.

    Args:
        subsystem: ``memory`` or ``skills``.
        args: tokens after the slash command (e.g. ``["approve", "a1b2"]``).
        memory_store: live MemoryStore for applying approved memory writes
            (CLI passes ``self.agent._memory_store``; gateway applies against a
            freshly loaded store).
        set_mode_fn: optional callable ``(enabled: bool) -> None`` that
            persists the new write_approval boolean to config (gateway provides
            this; CLI uses its own ``save_config_value`` and passes a closure).

    Returns a text string to show the user. Returns None when the args are not
    a write-approval subcommand (caller falls through to its other handling,
    e.g. /skills search).
    """
    if not args:
        # Bare /memory or /skills with no sub → show pending + gate state.
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)

    sub = args[0].lower()
    rest = args[1:]

    if sub == "pending":
        return _fmt_pending_list(subsystem)

    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store,
                        expected_payload_sha256=expected_payload_sha256)

    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest, expected_payload_sha256=expected_payload_sha256)

    if sub == "resolve":
        return _resolve_applying(subsystem, rest)

    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)

    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)

    return None  # not ours — caller handles


def _resolve_one(subsystem: str, rest: List[str]):
    if not rest:
        return None, f"Usage: /{subsystem} approve|reject <id>  (or 'all')"
    return rest[0], None


def _approve(subsystem: str, rest: List[str], memory_store,
             expected_payload_sha256: Optional[str] = None) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} approve <id>"

    if target.lower() == "all" and expected_payload_sha256 is not None:
        return (
            "expected_payload_sha256 cannot be combined with approve 'all': "
            "one digest binds exactly one pending record."
        )

    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."

    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return f"No pending {subsystem} write with id '{target}'."
        targets = [rec]

    applied, failed, overwritten, removed = 0, [], [], []
    for rec in targets:
        pending_id = rec["id"]
        applied_result = {}
        def _apply(current):
            nonlocal applied_result
            success, error, applied_result = _apply_one(subsystem, current, memory_store)
            return success, error
        # Root-skill reconciler replay: the digest is always provided (it is the
        # reviewer's seal); a CLI/gateway caller passes it only for a bound id.
        expected = (
            expected_payload_sha256 if expected_payload_sha256 is not None
            else rec.get("payload_sha256")
        )
        ok, msg = wa.apply_pending_record(
            subsystem,
            pending_id,
            _apply,
            expected_payload_sha256=expected,
        )
        if ok:
            applied += 1
            overwritten.extend(f"  {rec['id']}: {text}" for text in _changed_entries(applied_result, "replaced"))
            removed.extend(f"  {rec['id']}: {text}" for text in _changed_entries(applied_result, "removed"))
        else:
            failed.append(f"{pending_id}: {msg}")

    out = [f"Approved {applied} {subsystem} write(s)."]
    if overwritten:
        # A memory 'replace' overwrites the WHOLE matched entry (#117952); the approver
        # is the last person who can notice a clause went missing, so show what was lost.
        out.append("Overwrote entire entry (re-add anything you still need):")
        out.extend(overwritten)
    if removed:
        out.append("Removed entry (re-add anything you still need):")
        out.extend(removed)
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _changed_entries(result: dict, kind: str) -> List[str]:
    """Full text of every entry a memory replace overwrote (``kind="replaced"``) or remove
    deleted (``"removed"``), single-op or batch shape."""
    single = result.get(f"{kind}_entry")
    batch = result.get(f"{kind}_entries") or {}
    return ([single] if single else []) + [batch[k] for k in sorted(batch, key=int)]


def _matched_entries(payload) -> List[str]:
    """The full entry each staged memory replace/remove is pinned to: the summary shows only
    the old_text search string, and approval applies to this entry, not to that search."""
    from tools.memory_tool import destructive_ops
    return [f"{op['action']}s entry: {op['matched_entry']}" if op.get("matched_entry")
            else f"{op['action']}: unpinned legacy target \u2014 reject and recreate before approving"
            for op in destructive_ops(payload)]


def _apply_one(subsystem: str, rec, memory_store):
    """``(ok, error, result)`` — *result* is the applier's full payload (empty on exceptions)."""
    payload = rec.get("payload", {})
    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable", {}
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store)
            return bool(result.get("success")), result.get("error", ""), result
        else:
            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(apply_skill_pending(payload))
            return bool(result.get("success")), result.get("error", ""), result
    except Exception as e:
        return False, str(e), {}


def _reject(subsystem: str, rest: List[str], expected_payload_sha256: Optional[str] = None) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} reject <id>"
    if target.lower() == "all" and expected_payload_sha256 is not None:
        return "expected_payload_sha256 cannot be combined with reject 'all'."
    if target.lower() == "all":
        n = 0
        quarantined = []
        for rec in wa.list_pending(subsystem):
            if rec.get("state", "pending") == "applying":
                quarantined.append(rec["id"])
                continue
            if wa.discard_pending(subsystem, rec["id"]):
                n += 1
        out = [f"Rejected {n} pending {subsystem} write(s)."]
        if quarantined:
            out.append(
                "Not rejected; target reconciliation required for applying "
                "record(s): " + ", ".join(quarantined)
            )
        return "\n".join(out)
    rec = wa.get_pending(subsystem, target)
    if not rec:
        return f"No pending {subsystem} write with id '{target}'."
    if rec.get("state", "pending") == "applying":
        return (
            f"Pending {subsystem} write '{target}' is in applying state; "
            "reconcile its target before discarding the approval evidence."
        )
    # Exact-discard surface: with the reviewed digest bound, a record swapped
    # in under the same id is never consumed by an earlier rejection decision.
    expected = expected_payload_sha256 if expected_payload_sha256 is not None else rec.get("payload_sha256")
    if wa.discard_pending(subsystem, target, expected_payload_sha256=expected):
        return f"Rejected pending {subsystem} write '{target}'."
    return (
        f"Pending {subsystem} write '{target}' changed on disk (payload_sha256 "
        f"mismatch); rejected nothing — re-review it before discarding."
    )


def _resolve_applying(subsystem: str, rest: List[str]) -> str:
    if len(rest) != 2 or rest[1] not in {"applied", "not-applied"}:
        return (
            f"Usage: /{subsystem} resolve <id> applied|not-applied "
            "(only after inspecting the target)"
        )
    pending_id, resolution = rest
    ok, message = wa.resolve_applying(subsystem, pending_id, resolution)
    if ok:
        return f"Resolved pending {subsystem} write '{pending_id}': {message}."
    return f"Could not resolve pending {subsystem} write '{pending_id}': {message}."


def _diff(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills diff <id>"
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return f"No pending skill write with id '{rest[0]}'."
    diff = wa.skill_pending_diff(rec)
    header = f"# Pending skill write {rec['id']}: {rec.get('summary', '')}\n"
    return header + "\n" + diff


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem.

    ``set_mode_fn`` (when provided) persists the new boolean to config.
    """
    if not rest:
        return (f"{_fmt_state(subsystem)}\n"
                f"Set with: /{subsystem} approval <on|off>")
    arg = rest[0].strip().lower()
    truthy = {"on", "true", "yes", "1", "enable", "enabled"}
    falsey = {"off", "false", "no", "0", "disable", "disabled"}
    if arg in truthy:
        enabled = True
    elif arg in falsey:
        enabled = False
    else:
        return f"Invalid value '{arg}'. Use: on or off."
    if set_mode_fn is None:
        val = "true" if enabled else "false"
        return (f"To change the {subsystem} approval gate, run:\n"
                f"  hermes config set {subsystem}.write_approval {val}")
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return f"Failed to set {subsystem}.write_approval: {e}"
    return f"{subsystem}.write_approval set to '{'on' if enabled else 'off'}'."
