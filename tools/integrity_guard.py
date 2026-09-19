"""Effectful-payload integrity guard for tool dispatch.

The context compressor rewrites HISTORICAL tool-call arguments on the send path so
oversized payloads stop riding every request. Before this module, that rewrite left a
normal-looking prefix followed by the literal sentinel ``...[truncated]`` inside
otherwise executable JSON. Because the replay was not typed as non-authoritative, a
model on a long session copied the marker into a FRESH ``kanban_create`` call and the
arguments were faithfully executed and persisted — an incomplete execution contract
built from a compression preview (production evidence: tasks ``t_e7a190fc`` /
``t_d6569a19``, board ``internal``, 2026-09-19).

Defense in depth, class-level:

1. The compressor now replaces a compacted arguments document with a typed opaque
   reference (``_hermes_compacted_arguments_json`` in ``agent.context_compressor``).
2. This module refuses fresh state-bearing payloads that carry compaction-preview
   sentinels BEFORE the tool runs: no execution, no persistence, no side effects, and a
   durable typed diagnostic (field path / length / sha256 of the refusing value — never
   its content).

Field semantics matter: only write-shaped fields (file contents, patch replacements,
task bodies/titles, message bodies, cron prompts) are state-bearing. Query-shaped
fields on read-only tools (``search_files.pattern``, a ``grep`` command probing the
literal marker) must keep working — this is deliberately NOT a global word ban. For
free-form code/command fields (``terminal.command``, ``execute_code.code``) only the
full corruption fingerprint (long preview head + sentinel) refuses, so a command that
merely quotes the short literal still runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- Sentinel registry -----------------------------------------------------------------
# Exact literal the compressor historically injected after a 200-char head. Kept as a
# bare constant (not derived from the compressor) so the guard stays valid even if the
# compressor's wording changes: old compactions persist in replayed session history.
TRUNCATION_PREVIEW_SENTINEL = "...[truncated]"

# The compressor's typed opaque reference (agent.context_compressor). A fresh call
# carrying this kind marker is by definition a replay of a compaction preview.
COMPACTED_ARGS_KIND = "hermes_compacted_tool_arguments"

# Persisted preview sentinels from other context-reduction passes, refused in
# state-bearing fields only: the skill-prune placeholder in its bare form
# (agent/prompt_builder.py SKILLS_GUIDANCE) and its parametrized prefix
# (agent/context_compressor.py SKILL_PRUNED_MARKER_PREFIX), plus the persisted-output
# recovery block (tools/tool_result_storage.py PERSISTED_OUTPUT_TAG/_CLOSING_TAG).
SKILL_PRUNED_MARKER = "[SKILL_PRUNED]"
SKILL_PRUNED_PREFIX = "[SKILL_PRUNED:"
PERSISTED_OUTPUT_OPEN = "<persisted-output>"
PERSISTED_OUTPUT_CLOSE = "</persisted-output>"

# Corruption fingerprint: a long preview head immediately followed by the truncation
# sentinel. Only this full shape refuses inside free-form code/command fields, where a
# short bare literal is usually an intentional probe (grep/echo).
_CORRUPTION_FINGERPRINT_RE = re.compile(r".{80,}" + re.escape(TRUNCATION_PREVIEW_SENTINEL), re.DOTALL)

# Write-shaped string fields per tool: (tool name -> field names whose content becomes
# durable/external state). Table-driven (root shape rule); unknown tools fall back to
# the conservative universal scan below. Grounded in each tool's registered schema:
# kanban_create persists BOTH title and body (args.get("body") / str(title)); patch
# replacement payloads land in durable files; cronjob_manage persists create/update
# prompts. Query/selector fields are deliberately absent.
_STATE_FIELDS_BY_TOOL: Dict[str, Tuple[str, ...]] = {
    "write_file": ("content",),
    "patch": ("new_string",),
    "kanban_create": ("title", "body"),
    "kanban_comment": ("body",),
    "kanban_complete": ("summary", "result"),
    "kanban_request_review": ("summary",),
    "kanban_request_changes": ("reason",),
    "kanban_block": ("reason",),
    "kanban_heartbeat": ("note",),
    "send_message": ("message",),
    "cronjob_manage": ("prompt", "paused_reason", "name"),
}

# Free-form fields where only the full corruption fingerprint refuses (see module
# docstring): quoting the short literal in a command is a legitimate probe.
_FINGERPRINT_ONLY_FIELDS: Dict[str, Tuple[str, ...]] = {
    "terminal": ("command",),
    "execute_code": ("code",),
}

_SCAN_MAX_DEPTH = 24
_DIAGNOSTIC_VALUE_PREVIEW_LIMIT = 200


def _looks_like_compacted_reference(value: Any) -> bool:
    """True when a parsed value IS (or embeds) the compressor's typed opaque reference."""
    if not isinstance(value, dict):
        return False
    if value.get("kind") == COMPACTED_ARGS_KIND:
        return True
    return any(
        isinstance(v, dict) and v.get("kind") == COMPACTED_ARGS_KIND for v in value.values()
    )


def _sentinel_hits(value: str) -> List[str]:
    """Classify one string value against the sentinel registry."""
    hits: List[str] = []
    if (COMPACTED_ARGS_KIND in value
            or SKILL_PRUNED_MARKER in value
            or value.lstrip().startswith(SKILL_PRUNED_PREFIX)):
        hits.append("typed_preview_marker")
    if TRUNCATION_PREVIEW_SENTINEL in value:
        hits.append("truncation_sentinel")
    if PERSISTED_OUTPUT_OPEN in value and PERSISTED_OUTPUT_CLOSE in value:
        hits.append("persisted_output_block")
    return hits


def _classify_state_value(value: str, *, fingerprint_only: bool) -> List[str]:
    """Hits for a state-bearing value. ``fingerprint_only`` fields refuse only the full
    long-head+sentinel corruption fingerprint (probe compatibility)."""
    hits = _sentinel_hits(value)
    if fingerprint_only:
        bare = "truncation_sentinel" in hits
        # The typed markers refuse anywhere, even in commands: they are never
        # legitimate command content.
        typed = [h for h in hits if h != "truncation_sentinel"]
        if not bare:
            return typed
        if _CORRUPTION_FINGERPRINT_RE.search(value):
            return typed + ["truncation_fingerprint"]
        return typed
    return hits


def _iter_string_leaves(obj: Any, path: str, depth: int = 0) -> List[Tuple[str, str]]:
    """(field path, value) for every string leaf, depth-bounded. Paths are JSON-path
    shaped (``body``, ``tasks[2].goal``) so the diagnostic names the exact field."""
    out: List[Tuple[str, str]] = []
    if depth > _SCAN_MAX_DEPTH:
        return out
    if isinstance(obj, str):
        out.append((path or "<root>", obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_iter_string_leaves(v, f"{path}.{k}" if path else str(k), depth + 1))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(_iter_string_leaves(v, f"{path}[{i}]", depth + 1))
    return out


def _state_field_paths(tool_name: str) -> Tuple[Optional[Tuple[str, ...]], bool]:
    """``(field names or None-for-universal, fingerprint_only)`` for *tool_name*."""
    if tool_name in _STATE_FIELDS_BY_TOOL:
        return _STATE_FIELDS_BY_TOOL[tool_name], False
    if tool_name in _FINGERPRINT_ONLY_FIELDS:
        return _FINGERPRINT_ONLY_FIELDS[tool_name], True
    return None, False


def _diagnostic_entry(path: str, value: str, hits: List[str]) -> Dict[str, Any]:
    return {
        "field_path": path,
        "length": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
        "sentinels": hits,
        "head_preview": value[:_DIAGNOSTIC_VALUE_PREVIEW_LIMIT],
    }


def find_corrupted_payload(
    tool_name: str,
    function_args: Any,
) -> Optional[Dict[str, Any]]:
    """Locate compaction-preview contamination in a FRESH effectful tool payload.

    Returns ``None`` when nothing was found or the tool/field combination carries no
    durable state (read-only query fields stay usable for the literal marker). Found:
    ``{"reason", "field", "diagnostics", "tool", "length", "sha256"}`` — typed for a
    ``compacted_payload_refused`` tool error and secret-safe by construction (field
    path, length, hash, and a short head preview; never the full payload).
    """
    if not isinstance(function_args, dict):
        return None
    if _looks_like_compacted_reference(function_args):
        blob = json.dumps(function_args, ensure_ascii=False, default=str)
        return {
            "reason": "compacted_tool_arguments_reference",
            "field": "<arguments>",
            "diagnostics": [{"kind": COMPACTED_ARGS_KIND}],
            "tool": tool_name,
            "length": len(blob),
            "sha256": hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest(),
        }
    fields, fingerprint_only = _state_field_paths(tool_name)
    if fields is None:
        # Unknown/effect-unknown tool: conservative universal scan over every string
        # leaf (read-only tools are separately exempted by the caller).
        candidates = _iter_string_leaves(function_args, "")
    else:
        candidates = []
        for path, value in _iter_string_leaves(function_args, ""):
            root = path.split(".")[0].split("[")[0]
            if root in fields or path in fields:
                candidates.append((path, value))
    for path, value in candidates:
        hits = _classify_state_value(value, fingerprint_only=fingerprint_only)
        if hits:
            entry = _diagnostic_entry(path, value, hits)
            return {
                "reason": hits[0],
                "field": path,
                "diagnostics": [entry],
                "tool": tool_name,
                "length": entry["length"],
                "sha256": entry["sha256"],
            }
    return None


# Read-only tools: sentinel text in their arguments is DATA being queried, never state
# being written (spec section C: do not globally ban a word). Mirrors
# agent.tool_result_classification.NO_EFFECT_TOOL_NAMES plus the read/search pair.
READ_ONLY_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal", "tool_search",
    "tool_describe", "kanban_show", "kanban_attachments", "kanban_list",
})


def refusal_message(tool_name: str, finding: Dict[str, Any]) -> str:
    """Typed, actionable tool-error body for a refused payload."""
    field = finding.get("field", "?")
    return (
        f"compacted_payload_refused / incomplete_tool_payload: {tool_name}.{field} "
        f"contains a context-compaction preview marker ({finding.get('reason')}). "
        "The payload was reconstructed from a truncated in-context preview and is NOT "
        "authoritative; no tool ran and nothing was persisted. Fetch the canonical "
        "bytes from durable history (session store / artifact) and verify a SHA-256 "
        "digest before retrying with the intact payload."
    )


def guard_effectful_payload(tool_name: str, function_args: Any) -> Optional[str]:
    """One-call seam for dispatchers: ``tool_error``-shaped refusal JSON, or ``None``
    when the call is clean. Read-only tools always pass (marker-in-query stays usable).
    """
    if tool_name in READ_ONLY_TOOL_NAMES:
        return None
    finding = find_corrupted_payload(tool_name, function_args)
    if finding is None:
        return None
    logger.warning(
        "Effectful tool payload refused before execution (tool=%s, field=%s, "
        "length=%s, sha256=%s, sentinels=%s) — preview corruption blocked; full value "
        "NOT logged",
        tool_name, finding.get("field"), finding.get("length"), finding.get("sha256"),
        [d.get("sentinels") for d in finding.get("diagnostics", []) if isinstance(d, dict)],
    )
    return refusal_message(tool_name, finding)
