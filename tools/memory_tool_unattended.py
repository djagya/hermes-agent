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


_NO_DELETE_ADVICE = (
    " The background review cannot remove or shorten entries, so do not retry with a batch or a removal. "
    "Save nothing more to memory in this review; put a procedure or task lesson in the relevant skill instead.")


def _refusal(reason: str, usage: str) -> dict[str, Any]:
    return _error(reason + _NO_DELETE_ADVICE, "over_budget", done=True, usage=usage)


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
        return _refusal(f"Not saved: {label} is at {current:,}/{limit:,} chars and this would take it to "
                        f"{total:,}, past the {cap:,}-char limit for unattended background-review adds "
                        f"(memory.background_add_ceiling = {ratio:g}).", f"{current:,}/{limit:,}")

    result = store._mutate(target, _check, skip_drift=True)
    return None if result.get("success") else json.dumps(result, ensure_ascii=False)


def unattended_overflow_refusal(store: Any, target: str, result_json: str) -> str:
    """The ceiling check and the store's write take the lock separately, so a concurrent writer can
    still fill the store in between. The store's own over-limit refusal then asks for an
    add-plus-removal batch, which an unattended review cannot run (it would only be staged): give
    the review the no-removal refusal instead. Other callers get ``result_json`` unchanged."""
    from tools.skill_provenance import is_unattended_review

    if not is_unattended_review():
        return result_json
    label = "USER.md" if target == "user" else "MEMORY.md"
    usage = store._usage(target)
    return json.dumps(_refusal(f"Not saved: {label} is full for unattended background-review adds ({usage} chars).",
                               usage), ensure_ascii=False)
