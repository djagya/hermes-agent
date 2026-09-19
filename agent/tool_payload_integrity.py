"""Persist-before-execute integrity invariant for the current-turn execution envelope.

Spec (truncation-integrity, section A): the arguments handed to a tool handler must be
byte-exact with what the provider emitted and with what was persisted to the session DB
before execution. Compression and every other send-path rewrite operate on detached
copies only; a divergence at this boundary means the durable transcript no longer
matches what actually ran, so the turn stops before side effects.

The row-vs-handler comparison is keyed by tool-call id (the assistant row keeps every
emitted call, including mixed-batch invalid ones that are never dispatched — positional
indexing would misalign the two lists and stop turns spuriously). The handler leg
re-reads ``tc.function.arguments`` off the same provider response object the call was
parsed from, so any earlier in-place mutation of the pending response object surfaces
here as a row/handler divergence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Free-form fields where only the full corruption fingerprint refuses (probe
# compatibility with the sentinel literal inside commands/code). Mirrors
# tools.integrity_guard._FINGERPRINT_ONLY_FIELDS; kept literal here so the invariant
# does not import the dispatcher module.
_FINGERPRINT_ONLY_FIELDS = {
    "terminal": ("command",),
    "execute_code": ("code",),
}
_CORRUPTION_MIN_HEAD = 80

TRUNCATION_SENTINEL = "...[truncated]"


def _args_digest(raw_arguments: Any) -> Optional[str]:
    if not isinstance(raw_arguments, str):
        return None
    return hashlib.sha256(raw_arguments.encode("utf-8", errors="replace")).hexdigest()


def _looks_like_replayed_preview(function_name: str, value: str) -> bool:
    """True when a fresh call embeds the compaction preview corruption fingerprint —
    a long preview head immediately followed by the literal sentinel (the production
    incident shape in tasks t_e7a190fc / t_d6569a19)."""
    if function_name not in _FINGERPRINT_ONLY_FIELDS:
        return TRUNCATION_SENTINEL in value
    for leaf in _string_leaves(value):
        idx = leaf.find(TRUNCATION_SENTINEL)
        if idx >= _CORRUPTION_MIN_HEAD:
            return True
    return False


def _string_leaves(value: str) -> List[str]:
    """String leaves of a JSON-encoded arguments document (best effort)."""
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return [value]
    out: List[str] = []

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 24:
            return
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v, depth + 1)

    _walk(parsed)
    return out


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
    - ``pending_call_preview_corruption``: a pending call already carrying the compaction
      preview fingerprint would be persisted verbatim and replayed forever; refuse it
      before the store ever sees the corrupted form.

    Returns ``None`` when the envelope is intact. Otherwise a typed diagnostic —
    tool-call ids and digests only, never payload content — for a
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
        if digest is not None and _looks_like_replayed_preview(name, raw):
            mismatches.append({
                "boundary": "pending_call_preview_corruption",
                "call_id": call_id,
                "tool": name,
                "sentinel": TRUNCATION_SENTINEL,
            })
    if not mismatches:
        return None
    return {"error": "tool_payload_integrity", "mismatches": mismatches}
