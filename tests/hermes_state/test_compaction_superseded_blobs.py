"""In-place compaction stores a carried message's provider replay blobs once.

Each compaction supersedes the carried originals (``active=0, compacted=0``) and stores a live copy
that holds the same ``reasoning_details`` / ``codex_*_items`` bytes. Replay reads only active rows, so
the superseded original drops its own copy of those bytes; every compaction used to keep another.
A carried copy whose blobs differ leaves the original's blobs alone, and replay, the display
transcript and search stay as they were.
"""

import json

import pytest

from agent.conversation_compression_archive import coverage_for_commit
from hermes_state import SessionDB

SUMMARY = "summary of the earlier turns"
BLOBS = {
    "reasoning_details": [{"type": "reasoning.text", "text": "thinking about Q2"}],
    "codex_reasoning_items": [{"type": "reasoning", "id": "rs_1", "encrypted_content": "enc" * 200}],
    "codex_message_items": [{"type": "message", "id": "msg_1", "role": "assistant"}],
}


def _stored_blobs(db, row_id):
    row = db._conn.execute(
        "SELECT reasoning_details, codex_reasoning_items, codex_message_items FROM messages WHERE id = ?",
        (row_id,)).fetchone()
    return {column: json.loads(row[column]) if row[column] else None for column in BLOBS}


# path -> which archive_and_compact branch supersedes the original; carry_blobs=False: the compressor's
# carried copy lost its blobs, so the original is the only holder and must keep them.
@pytest.mark.parametrize("path, carry_blobs", [
    ("carried_tail", True),
    ("covered_ids", True),
    ("watermark_clone", True),
    ("carried_tail", False),
])
def test_superseded_original_keeps_blobs_only_when_its_live_copy_lacks_them(tmp_path, path, carry_blobs):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("sid", source="test")
        db.append_message("sid", "user", "Q1")
        db.append_message("sid", "assistant", "A1")
        held = db.get_messages_as_conversation("sid", include_row_ids=True)
        watermark = db.get_active_message_watermark("sid")
        db.append_message("sid", "user", "Q2")
        original = db.append_message("sid", "assistant", "A2", **BLOBS)
        if path == "watermark_clone":  # Q2/A2 landed during the summary call: cloned after it
            db.archive_and_compact("sid", [{"role": "user", "content": SUMMARY}], watermark=watermark)
        else:
            held = db.get_messages_as_conversation("sid", include_row_ids=True)
            tail = [dict(message) for message in held[-2:]]
            if not carry_blobs:
                for column in BLOBS:
                    tail[-1].pop(column, None)
            coverage = dict(zip(("covered_ids", "unresolved_held"), coverage_for_commit(db, "sid", held))) \
                if path == "covered_ids" else {}
            db.archive_and_compact("sid", [{"role": "user", "content": SUMMARY}, *tail], tail_count=2, **coverage)

        flags = db._conn.execute("SELECT active, compacted FROM messages WHERE id = ?", (original,)).fetchone()
        assert tuple(flags) == (0, 0)  # superseded by the live copy, not summarized away
        assert _stored_blobs(db, original) == (BLOBS if not carry_blobs else dict.fromkeys(BLOBS))

        replay = db.get_messages_as_conversation("sid")
        assert [m["content"] for m in replay] == [SUMMARY, "Q2", "A2"]
        live_blobs = {column: replay[-1].get(column) for column in BLOBS}
        assert live_blobs == (BLOBS if carry_blobs else dict.fromkeys(BLOBS))
        shown = [m["content"] for m in db.get_messages("sid", include_compacted=True)]
        assert shown.count("A2") == 1 and "A1" in shown
        assert len(db.search_messages("A2")) == 1
    finally:
        db.close()
