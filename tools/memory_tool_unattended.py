"""Write-time ceiling for memory adds made by the unattended background review.

The background review may add entries unattended but never remove them (#105921), so each
unattended add is a one-way step toward the hard cap, after which every foreground save fails.
``memory.background_add_ceiling`` (a ratio of the char limit; default 1.0 = the hard cap, i.e.
the previous behaviour) stops those adds below the cap and keeps the rest for foreground turns
(#135039, #106919).

The refusal is written for a writer that cannot delete: it asks the review to save nothing and
route procedures to skills. The store's own over-limit text asks for an add-plus-removal batch,
which the review cannot run (the batch is staged, not applied), so every retry used to stage
another proposal that nobody reviewed.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from tools.memory_tool_store import ENTRY_DELIMITER, _error

DEFAULT_CEILING = 1.0


def background_add_ceiling(config: Optional[dict[str, Any]] = None) -> float:
    """``memory.background_add_ceiling`` clamped to [0, 1]; invalid values use the default."""
    from tools.memory_tool import get_builtin_memory_config
    raw = get_builtin_memory_config(config).get("background_add_ceiling", DEFAULT_CEILING)
    try:
        ratio = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CEILING
    return min(max(ratio, 0.0), 1.0)


def unattended_add_refusal(store: Any, target: str, contents: list[Any]) -> Optional[str]:
    """JSON refusal when an unattended review add would take ``target`` past the ceiling, else
    None. Reads the live file under the store lock, so writes from other sessions count."""
    from tools.skill_provenance import is_unattended_review

    adds = [str(c).strip() for c in contents if c and str(c).strip()]
    if not adds or not is_unattended_review():
        return None
    ratio = background_add_ceiling()
    label = "USER.md" if target == "user" else "MEMORY.md"

    def _check(entries: list[str], limit: int) -> dict[str, Any]:
        new = [c for c in dict.fromkeys(adds) if c not in entries]  # duplicates are no-ops
        current, total = len(ENTRY_DELIMITER.join(entries)), len(ENTRY_DELIMITER.join(entries + new))
        cap = int(limit * ratio)
        if not new or total <= cap:
            return {"success": True}
        return _error(
            f"Not saved: {label} is at {current:,}/{limit:,} chars and this would take it to {total:,}, past "
            f"the {cap:,}-char limit for unattended background-review adds (memory.background_add_ceiling = "
            f"{ratio:g}). The background review cannot remove or shorten entries, so do not retry with a "
            "batch or a removal. Save nothing more to memory in this review; put a procedure or task lesson "
            "in the relevant skill instead.", "over_budget", done=True, usage=f"{current:,}/{limit:,}")

    result = store._mutate(target, _check, skip_drift=True)
    return None if result.get("success") else json.dumps(result, ensure_ascii=False)
