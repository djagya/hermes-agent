"""Outbound delivery receipts: persist platform delivery coordinates onto the exact
assistant row after a confirmed platform ACK, and never on failure/ambiguity.

Invariants (the full class, one delivery seam):
- A confirmed successful final send persists the primary and every split/rich
  remote message id (plus chat/thread identity and the delivered timestamp)
  onto the EXACT persisted assistant message it belongs to — addressed by the
  durable row id stamped at persist time, never by content/timestamp/"latest" guessing.
- A failed, ambiguous, or retried-without-new-ACK delivery never creates or
  overwrites a receipt; legacy rows without receipts stay exactly as they were.
- Telegram deep-link rendering is derived only from persisted coordinates and
  distinguishes private-supergroup-forum, non-forum, public-username, and
  unsupported shapes; anything underdetermined renders no link at all.
"""

import json
import time

import pytest

from gateway import delivery_ledger as dl
from gateway.platforms.base import SendResult
from gateway.platforms.telegram_link import telegram_message_link
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    return SessionDB(db_path=home / "state.db")


def _session_with_assistant_row(db):
    """A session with one durable assistant row; returns (session_id, row_id)."""
    db.create_session("sess-1", source="test")
    row_id = db.append_message(session_id="sess-1", role="assistant", content="final answer")
    return "sess-1", row_id


def _receipt(message_ids, chat_id, thread_id=None, chat_kind=None, chat_handle=None,
             delivered_at=None):
    return {
        "platform": "telegram",
        "chat_id": chat_id,
        "thread_id": thread_id,
        "message_ids": list(message_ids),
        "chat_kind": chat_kind,
        "chat_handle": chat_handle,
        "delivered_at": delivered_at if delivered_at is not None else time.time(),
    }


# ── Invariant 1: a confirmed send persists exact ids on the exact row ──────────


def test_confirmed_delivery_persists_all_ids_against_exact_row(db):
    session_id, row_id = _session_with_assistant_row(db)
    delivered_at = time.time()
    receipt = _receipt(["401", "402"], chat_id="-1004500", thread_id="17",
                       chat_kind="forum", delivered_at=delivered_at)

    changed = dl.record_delivery_receipt(
        db, session_id=session_id, row_id=row_id, receipt=receipt)

    assert changed == 1
    row = db._read_one(
        "SELECT platform_delivery FROM messages WHERE id = ? AND session_id = ?",
        (row_id, session_id))
    stored = json.loads(row["platform_delivery"])
    assert stored["platform"] == "telegram"
    assert stored["chat_id"] == "-1004500"
    assert stored["thread_id"] == "17"
    assert stored["message_ids"] == ["401", "402"]  # primary LAST, order preserved
    assert stored["delivered_at"] == delivered_at


def test_confirmed_delivery_read_back_through_get_messages(db):
    session_id, row_id = _session_with_assistant_row(db)
    dl.record_delivery_receipt(
        db, session_id=session_id, row_id=row_id,
        receipt=_receipt(["77"], chat_id="@public_handle", chat_kind="public",
                         chat_handle="@public_handle"))

    messages = db.get_messages(session_id)
    assert len(messages) == 1
    assert messages[0]["platform_delivery"]["message_ids"] == ["77"]
    assert messages[0]["platform_delivery"]["chat_handle"] == "@public_handle"


def test_telegram_links_for_every_supported_shape():
    # Private supergroup forum topic: /c/<internal>/<thread>/<id>
    assert telegram_message_link({"platform": "telegram", "chat_id": "-1004500",
        "thread_id": "17", "message_ids": ["401"], "chat_kind": "forum"}) == \
        "https://t.me/c/4500/17/401"
    # Private non-forum: /c/<internal>/<id>
    assert telegram_message_link({"platform": "telegram", "chat_id": "-1004500",
        "message_ids": ["402"], "chat_kind": "group"}) == \
        "https://t.me/c/4500/402"
    # Public username chat: @name/<id> (topic threads render without the thread segment)
    assert telegram_message_link({"platform": "telegram", "chat_id": "-1004500",
        "message_ids": ["403"], "chat_kind": "group", "chat_handle": "@cats"}) == \
        "https://t.me/cats/403"
    # Public DM with a bot username resolves on the bare id path
    assert telegram_message_link({"platform": "telegram", "chat_id": "88001",
        "message_ids": ["404"], "chat_kind": "dm", "chat_handle": "@mybot"}) == \
        "https://t.me/mybot/404"


@pytest.mark.parametrize("receipt", [
    {"platform": "slack", "chat_id": "C1", "message_ids": ["1"]},           # not telegram
    {"platform": "telegram", "message_ids": ["1"]},                          # no chat id
    {"platform": "telegram", "chat_id": "-1004500"},                         # no ids
    {"platform": "telegram", "chat_id": "-1004500", "message_ids": []},      # empty ids
    {"platform": "telegram", "chat_id": "not-a-chat", "message_ids": ["1"]}, # unusable chat id
])
def test_underdetermined_receipt_yields_no_link(receipt):
    assert telegram_message_link(receipt) is None


# ── Invariant 2: failure/ambiguity/retry never fabricate or overwrite ──────────


def test_failed_send_writes_no_receipt(db):
    session_id, row_id = _session_with_assistant_row(db)
    # The ledger marks the row failed; no receipt write may happen.
    dl.record_obligation(obligation_id="ob-f", session_key="k", platform="telegram",
                         chat_id="-1004500", thread_id=None, content="answer")
    dl.mark_failed("ob-f", "flood_control:60")

    row = db._read_one("SELECT platform_delivery FROM messages WHERE id = ?", (row_id,))
    assert row["platform_delivery"] is None


def test_ambiguous_attempting_send_writes_no_receipt(db):
    session_id, row_id = _session_with_assistant_row(db)
    dl.record_obligation(obligation_id="ob-a", session_key="k", platform="telegram",
                         chat_id="-1004500", thread_id=None, content="answer")
    dl.mark_attempting("ob-a")  # crashed mid-await: the platform MAY have it

    row = db._read_one("SELECT platform_delivery FROM messages WHERE id = ?", (row_id,))
    assert row["platform_delivery"] is None


def test_receipt_write_is_idempotent_and_never_overwritten_by_a_different_success(db):
    session_id, row_id = _session_with_assistant_row(db)
    first = _receipt(["500"], chat_id="-1004500", chat_kind="group", delivered_at=111.0)
    assert dl.record_delivery_receipt(
        db, session_id=session_id, row_id=row_id, receipt=first) == 1
    # A redelivery sweep "succeeding" again must not clobber the first confirmed receipt.
    second = _receipt(["900"], chat_id="-1004500", chat_kind="group", delivered_at=999.0)
    assert dl.record_delivery_receipt(
        db, session_id=session_id, row_id=row_id, receipt=second) == 0

    row = db._read_one("SELECT platform_delivery FROM messages WHERE id = ?", (row_id,))
    assert json.loads(row["platform_delivery"])["message_ids"] == ["500"]
    assert json.loads(row["platform_delivery"])["delivered_at"] == 111.0


def test_receipt_write_never_lands_on_a_neighbour_row(db):
    session_id, row_id = _session_with_assistant_row(db)
    other_id = db.append_message(session_id=session_id, role="assistant", content="later turn")
    assert other_id != row_id
    # Unknown row: nothing to address the receipt to — refused, not guessed onto another row.
    assert dl.record_delivery_receipt(
        db, session_id=session_id, row_id=999999, receipt=_receipt(["1"], chat_id="x")) == 0
    row = db._read_one("SELECT platform_delivery FROM messages WHERE id = ?", (other_id,))
    assert row["platform_delivery"] is None


def test_legacy_rows_without_receipts_stay_safe(db):
    session_id, row_id = _session_with_assistant_row(db)
    messages = db.get_messages(session_id)
    assert "platform_delivery" not in messages[0] or \
        messages[0]["platform_delivery"] is None
    assert telegram_message_link(messages[0].get("platform_delivery")) is None


# ── Invariant 3: the SendResult → receipt bridge at the egress seam ────────────


def test_sendresult_bridge_extracts_ids_and_coordinates(db):
    from gateway.platforms.base import build_delivery_receipt
    result = SendResult(
        success=True, message_id="702",
        raw_response={"message_ids": ["701", "702"]},
    )
    receipt = build_delivery_receipt(
        result, platform="telegram", chat_id="-1004500", thread_id="17",
        chat_kind="forum", chat_handle=None)
    assert receipt["message_ids"] == ["701", "702"]
    assert receipt["chat_id"] == "-1004500" and receipt["thread_id"] == "17"

    # Continuation-style split (message_id = LAST id, continuations in order).
    split = SendResult(success=True, message_id="803", continuation_message_ids=("802", "803"))
    receipt = build_delivery_receipt(
        split, platform="telegram", chat_id="-1004500", thread_id=None,
        chat_kind="group", chat_handle=None)
    assert receipt["message_ids"] == ["802", "803"]


def test_sendresult_bridge_refuses_failures(db):
    from gateway.platforms.base import build_delivery_receipt
    failed = SendResult(success=False, message_id="9", error="flood_control:60")
    assert build_delivery_receipt(
        failed, platform="telegram", chat_id="-1", thread_id=None,
        chat_kind="group", chat_handle=None) is None
    # An ACK-less "success" (empty-text no-op) carries no ids → no receipt.
    vacuous = SendResult(success=True, message_id=None)
    assert build_delivery_receipt(
        vacuous, platform="telegram", chat_id="-1", thread_id=None,
        chat_kind="group", chat_handle=None) is None
