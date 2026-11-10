"""Telegram approval-card identity and the owner-only payment card (payment design v2 A1/A4, Codex #1/#4).

Buttons carry a random single-use nonce bound to the card's request, chat, thread and message, so a
button from before an adapter restart, a replay, or another card's data resolves nothing. The
payment card goes only to the owner's private chat with every mandatory fact, or is not sent.
"""

import html
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import utf16_len
from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry
from tools.payment_consent_prompt import (
    GROUP_NOTICE, MAX_ITEM_TEXT, PAGE_MATCH_LINES, CartItem, PageMatch, PaymentPromptFacts, PromptIncomplete,
    ReceivingOrigin, StatedCart, render_html)

SESSION = "agent:main:telegram:group:-1001:5"
GROUP = -1001
OWNER_ID = 111
OWNER = f"telegram:{OWNER_ID}"


@pytest.fixture(autouse=True)
def _env():
    # Plain keyboard objects, so the buttons' callback data can be read back.
    button = lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data)
    markup = lambda rows: SimpleNamespace(inline_keyboard=rows)
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": f"{OWNER_ID},222"}), \
            patch("plugins.platforms.telegram.adapter.InlineKeyboardButton", button), \
            patch("plugins.platforms.telegram.adapter.InlineKeyboardMarkup", markup):
        yield
    with approval._lock:
        approval._gateway_queues.pop(SESSION, None)


def _adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t"))
    adapter._bot, adapter._app = AsyncMock(), MagicMock()
    sent = iter(range(40, 100))
    adapter._bot.send_message = AsyncMock(side_effect=lambda **kw: SimpleNamespace(
        message_id=next(sent), chat_id=kw["chat_id"], message_thread_id=kw.get("message_thread_id")))
    return adapter


def _pending(**data):
    entry = _ApprovalEntry({"command": "rm -rf build", **data})
    with approval._lock:
        approval._gateway_queues.setdefault(SESSION, []).append(entry)
    return entry


def _tap(adapter, data, *, chat_id, message_id, user_id=OWNER_ID, thread_id=None):
    query = AsyncMock()
    query.data = data
    query.message = SimpleNamespace(chat_id=chat_id, message_id=message_id, message_thread_id=thread_id,
                                    chat=SimpleNamespace(type="private" if chat_id > 0 else "group"), text_html="card")
    query.from_user = SimpleNamespace(id=user_id, first_name="U")
    return adapter._handle_callback_query(SimpleNamespace(callback_query=query), MagicMock()), query


async def _send_exec_card(adapter, entry):
    await adapter.send_exec_approval(chat_id=str(GROUP), command="rm -rf build", session_key=SESSION,
                                     request_id=entry.data["request_id"])
    kwargs = adapter._bot.send_message.call_args.kwargs
    data = kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    return data, adapter._bot.send_message.call_count + 39  # message_id of that send


@pytest.mark.asyncio
async def test_a_button_from_before_an_adapter_restart_resolves_nothing():
    """Codex #1: a restarted counter gave the first new card the old card's id, so the old button
    resolved a request its presser never saw."""
    old_request = _pending()
    old = _adapter()
    old_data, old_message = await _send_exec_card(old, old_request)
    assert len(old_data.encode()) <= 64
    with approval._lock:
        approval._gateway_queues[SESSION].remove(old_request)  # it settled before the restart
    new_request = _pending()
    restarted = _adapter()  # empty card map, as after a restart
    new_data, _ = await _send_exec_card(restarted, new_request)
    assert new_data != old_data
    coro, _ = _tap(restarted, old_data, chat_id=GROUP, message_id=old_message)
    await coro
    assert new_request.result is None and approval.has_blocking_approval(SESSION)


@pytest.mark.asyncio
async def test_card_binding_refuses_other_message_chat_thread_and_replay():
    adapter = _adapter()
    first, second = _pending(), _pending()
    data1, mid1 = await _send_exec_card(adapter, first)
    data2, mid2 = await _send_exec_card(adapter, second)
    assert data1 != data2
    for kwargs in ({"chat_id": GROUP, "message_id": mid2},          # card 1's data on card 2's message
                   {"chat_id": -2002, "message_id": mid1},          # another chat
                   {"chat_id": GROUP, "message_id": mid1, "thread_id": 9}):  # another thread
        coro, _ = _tap(adapter, data1, **kwargs)
        await coro
    assert first.result is None and second.result is None
    coro, _ = _tap(adapter, data1, chat_id=GROUP, message_id=mid1)
    await coro
    assert first.result == "once" and second.result is None  # only its own request
    with approval._lock:
        approval._gateway_queues[SESSION].append(first)  # even if it were pending again
    first.result = None
    coro, _ = _tap(adapter, data1, chat_id=GROUP, message_id=mid1)  # replay
    await coro
    assert first.result is None


def _facts(**over):
    base = dict(ref="c" * 32, card_label="Test card", merchant_origin="https://shop.example",
                receiving=(ReceivingOrigin("https://shop.example", "merchant", ("cc-name",)),
                           ReceivingOrigin("https://pay.psp.example", "reviewed", ("cc-number", "cc-csc"))),
                expires_in_s=97)
    return PaymentPromptFacts(**{**base, **over})


@pytest.mark.asyncio
async def test_payment_card_goes_to_the_owners_dm_and_only_the_owner_answers():
    adapter = _adapter()
    entry = _pending(exact_request=True, approver=OWNER)
    rid = entry.data["request_id"]
    result = await adapter.send_payment_approval(chat_id=str(OWNER_ID), facts=_facts(), request_id=rid,
                                                 session_key=SESSION, approver=OWNER)
    assert result.success
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == OWNER_ID and kwargs["parse_mode"] == "HTML"
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["Approve fill once", "Deny"]
    approve = buttons[0].callback_data
    mid = int(result.message_id)
    # Another (authorized) user presses: refused, card still live; typed /approve: nothing.
    coro, query = _tap(adapter, approve, chat_id=OWNER_ID, message_id=mid, user_id=222)
    await coro
    assert entry.result is None and "owner" in query.answer.call_args.kwargs["text"]
    assert approval.resolve_gateway_approval(SESSION, "once") == 0
    # A forged scope on the payment card is refused.
    coro, _ = _tap(adapter, approve.replace("pa:once:", "pa:always:"), chat_id=OWNER_ID, message_id=mid)
    await coro
    assert entry.result is None
    coro, _ = _tap(adapter, approve, chat_id=OWNER_ID, message_id=mid)
    await coro
    assert entry.result == "once"
    # A card for anyone but the approver's own private chat is never sent.
    refused = await adapter.send_payment_approval(chat_id=str(GROUP), facts=_facts(), request_id=rid,
                                                  session_key=SESSION, approver=OWNER)
    assert not refused.success


def _longest_origin(tag: str) -> str:
    host = ".".join([tag + "a" * (63 - len(tag)), "b" * 63, "c" * 63, "d" * 61])
    assert len(host) == 253  # the DNS maximum
    return "https://" + host


@pytest.mark.parametrize("state", ["matched", "not_found", "ambiguous", "not_checked"])
def test_rendered_card_has_every_fact_verbatim_or_raises(state):
    origins = [_longest_origin(f"p{i}") for i in range(2)]
    items = tuple(CartItem(f"Item {i} " + "&<x>" + "w" * (MAX_ITEM_TEXT - 12), i + 1) for i in range(8))
    facts = _facts(merchant_origin=origins[0], unreviewed=True,
                   receiving=(ReceivingOrigin(origins[0], "merchant", ("cc-name",)),
                              ReceivingOrigin(origins[1], "UNREVIEWED", ("cc-number", "cc-csc", "cc-exp"))),
                   cart=None if state == "not_checked" else StatedCart(items, "1234.50", "EUR"),
                   page_match=PageMatch(state, "" if state == "not_checked" else "https://shop.example"))
    text = render_html(facts, budget_utf16=4096)
    assert utf16_len(text) <= 4096
    plain = html.unescape(text)
    cart_facts = () if state == "not_checked" else ("1234.50 EUR", "stated by Sera", *(i.description for i in items))
    for fact in (origins[0], origins[1], "UNREVIEWED", "cc-number, cc-csc, cc-exp", "Test card",
                 PAGE_MATCH_LINES[state], "Expires in 97 s", "c" * 32, "Hermes will not press Pay",
                 "cannot verify what will be charged", *cart_facts):
        assert fact in plain, fact
    assert "3-D Secure" not in plain and "3DS" not in plain
    with pytest.raises(PromptIncomplete):
        render_html(facts, budget_utf16=utf16_len(text) - 1)  # never truncated to fit


@pytest.mark.asyncio
async def test_a_card_that_cannot_show_every_fact_is_not_sent():
    adapter = _adapter()
    bad_origin = _facts(merchant_origin="https://shop.example/path?x")  # not a plain origin
    result = await adapter.send_payment_approval(chat_id=str(OWNER_ID), facts=bad_origin, request_id="d" * 32,
                                                 session_key=SESSION, approver=OWNER)
    assert not result.success and adapter._bot.send_message.call_count == 0
    with pytest.raises(ValueError):
        CartItem("x" * (MAX_ITEM_TEXT + 1), 1)  # bounded before rendering, never cut
    assert CartItem("a\u202eb`c\nd", 1).description == "a b c d"  # bidi, backtick, newline removed


def _turn_runner(adapter, chat_id):
    import concurrent.futures
    from gateway.run_turn_runner import TurnRunner

    loop_results = []

    def schedule(coro, _msg, loop=None):
        import asyncio
        fut = concurrent.futures.Future()
        try:
            fut.set_result(asyncio.run(coro))
        except Exception as exc:  # surface to the caller like the real future
            fut.set_exception(exc)
        loop_results.append(fut)
        return fut

    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(_status_adapter=adapter, _status_chat_id=str(chat_id), session_key=SESSION,
                                  _status_thread_metadata={"thread_id": "5"})
    runner._approval_owner = None
    runner._schedule = schedule
    return runner


def test_group_session_gets_only_a_neutral_notice_and_never_the_text_fallback(monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "payment_approver", lambda: OWNER)
    adapter = MagicMock(spec=["send_payment_approval", "retire_payment_card", "send", "send_exec_approval"])
    type(adapter).send_payment_approval = MagicMock()  # class-level, as the delivery check requires
    adapter.send_payment_approval = AsyncMock(return_value=SimpleNamespace(success=True, message_id="9"))
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    entry = _pending(exact_request=True, approver=OWNER, payment=_facts().to_dict())
    runner = _turn_runner(adapter, GROUP)
    runner._approval_notify_sync(dict(entry.data))
    call = adapter.send_payment_approval.call_args.kwargs
    assert call["chat_id"] == str(OWNER_ID) and call["approver"] == OWNER and call["request_id"] == entry.data["request_id"]
    (notice,) = adapter.send.call_args_list
    assert notice.args[:2] == (str(GROUP), GROUP_NOTICE)
    adapter.send_exec_approval.assert_not_called()


@pytest.mark.parametrize("case", ["no_card_adapter", "bad_request_id", "send_failed", "approver_changed"])
def test_undeliverable_payment_card_raises_so_the_prompt_is_unavailable(monkeypatch, case):
    from gateway.payment_approval import PaymentCardUndeliverable
    from tools import approval_context
    monkeypatch.setattr(approval_context, "payment_approver", lambda: "telegram:999" if case == "approver_changed" else OWNER)

    class Plain:  # e.g. a Discord session adapter: no payment card
        send = AsyncMock()
        send_exec_approval = AsyncMock()

    class Card(Plain):
        send_payment_approval = AsyncMock(return_value=SimpleNamespace(success=case != "send_failed"))
        retire_payment_card = AsyncMock()

    adapter = Plain() if case == "no_card_adapter" else Card()
    data = dict(_pending(exact_request=True, approver=OWNER, payment=_facts().to_dict()).data)
    if case == "bad_request_id":
        data["request_id"] = ""
    with pytest.raises(PaymentCardUndeliverable):
        _turn_runner(adapter, GROUP)._render_approval_sync(data)
    Plain.send.assert_not_called()
    Plain.send_exec_approval.assert_not_called()
