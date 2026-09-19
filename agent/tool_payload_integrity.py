"""Persist-before-execute integrity invariant for the current-turn execution envelope.

Spec (truncation-integrity, section A): the arguments handed to a tool handler must be
byte-exact with what the provider emitted and with what was persisted to the session DB
before execution. Compression and every other send-path rewrite operate on detached
copies only; a divergence at this boundary means the durable transcript no longer
matches what actually ran, so the turn stops before side effects.

Ordering: the caller (``agent.turn_tool_round``) flushes the assistant row to the
session DB BEFORE running this invariant, so a pending call that already carries preview
corruption is durable here — kept as verbatim transcript, which is safe because replayed
history is a typed opaque reference (spec section B) and can no longer teach the model a
half-executable payload shape. This module refuses the EXECUTION of such a call; it does
not rewrite the transcript row.

The row-vs-handler comparison is keyed by tool-call id (the assistant row keeps every
emitted call, including mixed-batch invalid ones that are never dispatched — positional
indexing would misalign the two lists and stop turns spuriously). The handler leg
re-reads ``tc.function.arguments`` off the same provider response object the call was
parsed from, so any earlier in-place mutation of the pending response object surfaces
here as a row/handler divergence.

Corruption classification of pending calls is delegated to
``tools.integrity_guard.pending_call_corruption`` — the same effect/field classification
the dispatcher enforces (read-only tools stay usable for the literal ``...[truncated]``
marker; command/code fields refuse only the long-head+sentinel fingerprint; typed
preview markers refuse anywhere). One shared classification keeps this boundary and the
dispatcher from ever disagreeing about a payload (spec section C).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from tools.integrity_guard import pending_call_corruption

logger = logging.getLogger(__name__)


def _args_digest(raw_arguments: Any) -> Optional[str]:
    if not isinstance(raw_arguments, str):
        return None
    return hashlib.sha256(raw_arguments.encode("utf-8", errors="replace")).hexdigest()


def verify_persist_before_execute(
    assistant_msg: Dict[str, Any],
    parsed_calls: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Verify raw == persisted == to-execute at the persist-before-execute boundary.

    ``assistant_msg`` is the row that was just flushed to the session DB; its
    ``tool_calls`` entries carry the provider arguments verbatim
    (``chat_completion_helpers._assistant_tool_call_dict`` — deliberately unredacted
    and unrewritten). ``parsed_calls`` are the ``{"id", "name", "arguments"}`` dicts
    about to be handed to the dispatcher, in dispatch order.

    Two checks, both honest:

    - ``assistant_row_vs_handler``: for every DISPATCHED call, the row's arguments for
      the same tool-call id must be byte-identical (SHA-256) to what the handler is
      about to receive. This is the persisted-vs-executed leg of the spec invariant.
    - ``pending_call_preview_corruption``: a pending call classified corrupt by the
      dispatcher's own refusal logic (``tools.integrity_guard``) would run tools from a
      compaction preview if dispatch proceeded; stop the turn here instead. The
      corrupted call is already persisted as transcript at this point (the flush
      precedes this check) — that is safe, not a gap: history is now a typed opaque
      reference, so the row can never be replayed as a half-executable payload.

    Returns ``None`` when the envelope is intact. Otherwise a typed diagnostic —
    tool-call ids, digests, field paths; never payload content — for a
    ``session_persistence_failed``-style turn stop.
    """
    mismatches: List[Dict[str, Any]] = []
    row_by_id: Dict[str, Dict[str, Any]] = {}
    if isinstance(assistant_msg, dict):
        for tc in assistant_msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                call_id = tc.get("id") or tc.get("call_id") or ""
                row_by_id[str(call_id)] = fn if isinstance(fn, dict) else {}
    for parsed in parsed_calls or []:
        name = parsed.get("name", "?")
        call_id = str(parsed.get("id") or "")
        raw = parsed.get("arguments", "")
        digest = _args_digest(raw)
        row_args = row_by_id.get(call_id, {}).get("arguments") if call_id else None
        if row_args is not None and _args_digest(row_args) != digest:
            mismatches.append({
                "boundary": "assistant_row_vs_handler",
                "call_id": call_id,
                "tool": name,
                "row_sha256": _args_digest(row_args),
                "handler_sha256": digest,
            })
        if digest is not None:
            finding = pending_call_corruption(name, raw)
            if finding is not None:
                mismatches.append({
                    "boundary": "pending_call_preview_corruption",
                    "call_id": call_id,
                    "tool": name,
                    "reason": finding.get("reason"),
                    "field": finding.get("field"),
                    "length": finding.get("length"),
                    "sha256": finding.get("sha256"),
                })
    if not mismatches:
        return None
    return {"error": "tool_payload_integrity", "mismatches": mismatches}
