"""Hygiene for memory proposals the background review stages in ``pending/memory``.

The unattended review may not patch or remove entries, so it stages them (#105921). Nothing
bounded that queue: proposals never expired (#117894), and each review re-staged the same
consolidation because it could not see what it had already proposed (#145377). Three rules:

- **TTL:** a review proposal older than ``memory.pending_ttl_days`` (default 14, 0 = never) is
  discarded with a log line. Foreground-staged writes are the user's own and never expire.
- **Latest wins:** a newly staged review proposal replaces older review proposals on the same
  store whose pinned entries it covers. An older record that also targets other entries stays,
  so no unrelated intent is dropped.
- **Reviewer view:** the review prompt lists the review's own pending proposals, so it can
  leave them alone instead of re-proposing them.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from tools import write_approval as wa

logger = logging.getLogger(__name__)

REVIEW_ORIGIN = "background_review"
DEFAULT_TTL_DAYS = 14
_DIGEST_MAX_RECORDS = 10
_DIGEST_MAX_OPS = 3
_DIGEST_OP_CHARS = 90


def pending_ttl_seconds(config: Optional[dict[str, Any]] = None) -> float:
    """``memory.pending_ttl_days`` in seconds; 0 disables expiry. Invalid values use the default."""
    if config is None:
        from hermes_cli.config import load_config
        config = load_config()
    section = config.get("memory") if isinstance(config, dict) else None
    raw = section.get("pending_ttl_days", DEFAULT_TTL_DAYS) if isinstance(section, dict) else DEFAULT_TTL_DAYS
    try:
        days = float(raw)
    except (TypeError, ValueError):
        days = DEFAULT_TTL_DAYS
    return max(days, 0.0) * 86400


def _review_records() -> list[dict[str, Any]]:
    return [r for r in wa.list_pending(wa.MEMORY) if r.get("origin") == REVIEW_ORIGIN]


def expire_review_proposals(*, now: Optional[float] = None, ttl_seconds: Optional[float] = None) -> list[str]:
    """Discard review proposals older than the TTL; return their ids."""
    ttl = pending_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if ttl <= 0:
        return []
    now = time.time() if now is None else now
    expired = []
    for record in _review_records():
        age = now - float(record.get("created_at") or now)
        if age > ttl and wa.discard_pending(wa.MEMORY, record["id"]):
            expired.append(record["id"])
            logger.info("Expired background-review memory proposal %s after %.1f days unapproved: %s",
                        record["id"], age / 86400, (record.get("summary") or "")[:160])
    return expired


def _pinned_entries(payload: dict[str, Any]) -> frozenset[str]:
    from tools.memory_tool import destructive_ops
    return frozenset(op["matched_entry"] for op in destructive_ops(payload) if op.get("matched_entry"))


def supersede_review_proposals(record: dict[str, Any]) -> list[str]:
    """After ``record`` was staged by the review, discard older review proposals on the same
    store whose pinned entries are all targeted by ``record``; return their ids."""
    payload = record.get("payload") or {}
    pins = _pinned_entries(payload)
    if record.get("origin") != REVIEW_ORIGIN or not pins:
        return []
    superseded = []
    for older in _review_records():
        older_payload = older.get("payload") or {}
        if older["id"] == record["id"] or older_payload.get("target") != payload.get("target"):
            continue
        older_pins = _pinned_entries(older_payload)
        if older_pins and older_pins <= pins and wa.discard_pending(wa.MEMORY, older["id"]):
            superseded.append(older["id"])
            logger.info("Background-review memory proposal %s superseded by %s (same entries)",
                        older["id"], record["id"])
    return superseded


def after_review_stage(record: dict[str, Any]) -> dict[str, Any]:
    """Queue hygiene right after the review staged ``record``: expire, then collapse onto it.
    Returns fields for the tool result (empty when nothing was dropped)."""
    try:
        expired = expire_review_proposals()
        superseded = supersede_review_proposals(record)
    except Exception:  # hygiene must never fail the staged write
        logger.warning("Pending memory proposal hygiene failed", exc_info=True)
        return {}
    return {k: v for k, v in (("superseded", superseded), ("expired", expired)) if v}


def _clip(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= _DIGEST_OP_CHARS else text[: _DIGEST_OP_CHARS - 1] + "…"


def _op_phrase(op: dict[str, Any]) -> str:
    action = op.get("action", "?")
    if action == "add":
        return f"add «{_clip(op.get('content'))}»"
    entry = _clip(op.get("matched_entry") or op.get("old_text"))
    if action == "remove":
        return f"remove «{entry}»"
    return f"{action} «{entry}» → «{_clip(op.get('content'))}»"


def _record_line(record: dict[str, Any], now: float) -> str:
    payload = record.get("payload") or {}
    ops = (payload.get("operations") or []) if payload.get("action") == "batch" else [payload]
    age_days = max(0, int((now - float(record.get("created_at") or now)) // 86400))
    phrases = [_op_phrase(op or {}) for op in ops[:_DIGEST_MAX_OPS]]
    if len(ops) > _DIGEST_MAX_OPS:
        phrases.append(f"+{len(ops) - _DIGEST_MAX_OPS} more op(s)")
    return f"- {record['id']} ({age_days}d, {payload.get('target', 'memory')}): " + "; ".join(phrases)


def review_pending_digest() -> str:
    """Prompt suffix listing the review's own pending memory proposals ('' when none)."""
    try:
        expire_review_proposals()
        records = _review_records()
    except Exception:
        logger.debug("Could not read pending memory proposals for the review prompt", exc_info=True)
        return ""
    if not records:
        return ""
    now = time.time()
    shown = records[-_DIGEST_MAX_RECORDS:]
    lines = [_record_line(r, now) for r in reversed(shown)]
    if len(records) > len(shown):
        lines.append(f"- … {len(records) - len(shown)} older proposal(s) not shown")
    return ("\n\nMemory proposals you staged earlier are still waiting for the user's approval "
            f"({len(records)}, newest first). They are already queued: do not stage them again. "
            "A new patch/remove that targets the same entries replaces the older proposal, so "
            "propose one only if it is materially different.\n" + "\n".join(lines))
