"""In-place compaction stores a carried message's provider replay blobs once.

Each compaction supersedes the carried originals (``active=0, compacted=0``) and stores a live copy of
them, so every compaction a message survived kept one more byte copy of its ``reasoning_details``,
``codex_reasoning_items`` and ``codex_message_items``. Replay reads active rows only, and the display,
search and removed-identity reads never select these columns, so the superseded original drops them,
but only when a live row of the same ``message_uid`` holds byte-identical values of all three. A
carried copy whose blobs differ (the compressor dropped or rewrote them) leaves the original's alone.
The row itself, its content, flags, display identity and FTS entries are untouched.
"""

from __future__ import annotations

from hermes_state_common import _placeholders

_BLOB_COLUMNS = ("reasoning_details", "codex_reasoning_items", "codex_message_items")
_DROP_SUPERSEDED_BLOBS_SQL = (
    f"UPDATE messages SET {', '.join(f'{c} = NULL' for c in _BLOB_COLUMNS)} "
    "WHERE session_id = ? AND active = 0 AND compacted = 0 AND id IN ({ids}) "
    f"AND ({' OR '.join(f'{c} IS NOT NULL' for c in _BLOB_COLUMNS)}) "
    "AND EXISTS (SELECT 1 FROM messages AS live WHERE live.session_id = messages.session_id "
    "AND live.active = 1 AND live.id > messages.id AND live.message_uid = messages.message_uid"
    f"{''.join(f' AND live.{c} IS messages.{c}' for c in _BLOB_COLUMNS)})")


def drop_superseded_blobs(conn, session_id: str, superseded_ids: list[int]) -> None:
    """Clear the provider replay blobs of *superseded_ids* (the rows this compaction just superseded, on its
    own write transaction) whose live copy holds the same bytes."""
    if superseded_ids:
        conn.execute(_DROP_SUPERSEDED_BLOBS_SQL.format(ids=_placeholders(superseded_ids)),
                     [session_id, *superseded_ids])
