"""dlz: v0.23.1's approval actor model next to the fork's owner-only card-fill approval and fail-closed albums.

- Upstream binds an exec approval to the turn's actor (queued follow-ups, steers, mixed-sender turns with
  no single actor) and lets ``approvals.approvers`` narrow who answers. A card fill stays bound to
  ``approvals.payment_approver`` whatever that actor is: only the owner's own ``pa:`` press approves it,
  and no other surface (typed ``/approve`` ids, any platform's card tap with an actor) can, even when
  the actor check passes or names nobody.
- A refused ``ea:`` tap (not the requester) leaves the nonce card answerable by its requester.
- A ``_require_native_media_group`` set never falls back to loose photos on upstream's album sender.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.gateway.test_approval_requester_binding import DEFAULT, write_approvers
from tools import approval

SESSION = "agent:main:telegram:group:-1001:5"
OWNER_ID = "111"
OWNER = f"telegram:{OWNER_ID}"


@pytest.fixture(autouse=True)
def _clean():
    approval._gateway_queues.clear()
    yield
    approval._gateway_queues.clear()


def _queue(data, actor_id, actor_name=""):
    """Enqueue an approval raised inside a turn whose actor is *actor_id* (``""``: mixed senders)."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval_gateway_wait import _ApprovalEntry

    tokens = set_session_vars(platform="telegram", user_id=actor_id or "bob", user_name=actor_name, session_key=SESSION,
                              chat_type="group", actor_id=actor_id, actor_name=actor_name)
    try:
        entry = _ApprovalEntry(data)
    finally:
        clear_session_vars(tokens)
    entry.card_deny_only = True  # as ``_await_gateway_decision`` publishes every request
    with approval._lock:
        approval._gateway_queues.setdefault(SESSION, []).append(entry)
    return entry


@pytest.mark.parametrize("approvers", [DEFAULT, []])
@pytest.mark.parametrize("actor", ["bob", OWNER_ID, ""])  # another member, the owner, no single actor
def test_card_fill_is_bound_to_the_owner_whatever_the_turn_actor(approvers, actor):
    from gateway.approval_actor import answerable_request_ids

    write_approvers(approvers)
    entry = _queue({"command": "fill", "exact_request": True, "approver": OWNER}, actor)
    rid = entry.data["request_id"]
    # Any platform's card tap carrying a clicker (the requester, the owner by id, an admin): never approves.
    for clicker in ("bob", OWNER_ID):
        assert approval.resolve_gateway_approval_card(SESSION, "once", rid, actor_id=clicker, admin_ids=(clicker,)) == 0
    # The Telegram ``ea:`` path passes the owner's principal too, but a payment never has an ``ea:`` card.
    assert approval.resolve_gateway_approval_card(SESSION, "once", rid, actor_id=OWNER_ID, approver=OWNER) == 0
    # Typed /approve resolves exactly the ids the actor check returned: still nothing.
    ids, _ = answerable_request_ids(SESSION, OWNER_ID, admin_ids=(OWNER_ID,), resolve_all=True)
    assert sum(approval.resolve_gateway_approval(SESSION, "always", request_id=i) for i in ids) == 0
    assert entry.result is None and approval.has_blocking_approval(SESSION)
    # Only the owner's own payment-card press (``pa:`` -> resolve_gateway_approval with the principal).
    assert approval.resolve_gateway_approval(SESSION, "once", request_id=rid, approver="telegram:222") == 0
    assert approval.resolve_gateway_approval(SESSION, "once", request_id=rid, approver=OWNER) == 1
    assert entry.result == "once"


def _make_adapter():
    from tests.gateway.test_telegram_approval_buttons import _make_adapter as make
    return make()


def _ea_query(nonce, user_id, *, expired=False):
    from telegram.error import BadRequest

    query = AsyncMock(data=f"ea:once:{nonce}")
    query.message = SimpleNamespace(chat_id=1, message_id=7, message_thread_id=None, text_html="x",
                                    chat=SimpleNamespace(type="group"))
    query.from_user = SimpleNamespace(id=user_id, first_name=user_id.title())
    if expired:
        query.answer.side_effect = BadRequest("Query is too old and response timeout expired or query id is invalid")
    return query


@pytest.mark.asyncio
async def test_a_refused_ea_tap_leaves_the_card_to_its_requester(monkeypatch):
    write_approvers([])
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
    alice = _queue({"command": "rm -rf build"}, "alice", "Alice")
    approval.restrict_gateway_approval_card_to_deny(SESSION, alice.data["request_id"], restricted=False)
    adapter = _make_adapter()
    adapter._arm_approval_card("n1", "ea", SimpleNamespace(chat_id=1, message_id=7), chat_id="1", session_key=SESSION,
                               request_id=alice.data["request_id"], choices=("once", "deny"))
    bob = _ea_query("n1", "bob", expired=True)  # the refusal toast on an expired query must not raise
    await adapter._handle_callback_query(MagicMock(callback_query=bob), MagicMock())
    assert bob.answer.await_args.kwargs.get("show_alert") is True
    assert not alice.event.is_set() and "n1" in adapter._approval_cards
    await adapter._handle_callback_query(MagicMock(callback_query=_ea_query("n1", "alice")), MagicMock())
    assert alice.result == "once" and "n1" not in adapter._approval_cards


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "split"])
async def test_a_required_native_album_never_falls_back_to_loose_photos(tmp_path, outcome):
    from telegram.error import TimedOut

    photos = []
    for i in range(2):
        path = tmp_path / f"p{i}.png"
        path.write_bytes(b"png")
        photos.append((path.resolve().as_uri(), ""))
    adapter = _make_adapter()
    receipts = [SimpleNamespace(media_group_id="g1"), SimpleNamespace(media_group_id="g2")]
    adapter._bot.send_media_group = AsyncMock(side_effect=TimedOut("t") if outcome == "timeout" else None,
                                              return_value=receipts)
    adapter.send_image_file = AsyncMock()
    adapter.send_image = AsyncMock()
    with patch.object(adapter, "_compress_image_to_jpeg", return_value=None):
        result = await adapter.send_multiple_images("1", photos, metadata={"_require_native_media_group": True})
        assert not result.success
        assert adapter._bot.send_media_group.await_count == 1
        adapter.send_image_file.assert_not_awaited()
        adapter.send_image.assert_not_awaited()
        # Control: without the mark the same refusal-free split album is upstream's success.
        adapter._bot.send_media_group = AsyncMock(return_value=receipts)
        assert (await adapter.send_multiple_images("1", photos)).success
    assert os.path.exists(photos[0][0][7:])


@pytest.mark.asyncio
@pytest.mark.parametrize("presser", ["222", OWNER_ID])
async def test_a_payment_card_tap_on_an_expired_query_never_raises(monkeypatch, presser):
    """Telegram refusing the answer of an expired query must not escape the ``pa:`` handler: a stranger's
    press still leaves the card pending, the owner's press still settles it and drops the buttons."""
    from telegram.error import BadRequest

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER_ID},222")
    entry = _queue({"command": "fill", "exact_request": True, "approver": OWNER}, "bob")
    adapter = _make_adapter()
    adapter._arm_approval_card("p1", "pa", SimpleNamespace(chat_id=int(OWNER_ID), message_id=9), chat_id=OWNER_ID,
                               session_key=SESSION, request_id=entry.data["request_id"], choices=("once", "deny"),
                               approver=OWNER, text="card")
    query = AsyncMock(data="pa:once:p1")
    query.message = SimpleNamespace(chat_id=int(OWNER_ID), message_id=9, message_thread_id=None, text_html="card",
                                    chat=SimpleNamespace(type="private"))
    query.from_user = SimpleNamespace(id=int(presser), first_name="U")
    query.answer.side_effect = BadRequest("Query is too old and response timeout expired or query id is invalid")
    await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())
    if presser == OWNER_ID:
        assert entry.result == "once" and "p1" not in adapter._approval_cards
        query.edit_message_text.assert_awaited()
    else:
        assert entry.result is None and "p1" in adapter._approval_cards
