"""Telegram approval-card identity and the owner-only payment card (payment design v2 A1/A4, Codex #1/#4).

Buttons carry a random single-use nonce bound to the card's request, chat, thread and message, so a
button from before an adapter restart, a replay, or another card's data resolves nothing. The
payment card goes to the owner's private chat or a group chat (never someone else's private chat) with every
mandatory fact, or is not sent.
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
    APPROVE_LABEL, DENY_LABEL, GROUP_NOTICE, MAX_ITEM_TEXT, PAGE_MATCH_LINES, CartItem, PageMatch, PaymentPromptFacts, PromptIncomplete,
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
    stated = _facts(cart=StatedCart((CartItem("Ticket", 1),), "12.50", "CHF"), page_match=PageMatch("matched", "https://shop.example"))
    result = await adapter.send_payment_approval(chat_id=str(OWNER_ID), facts=stated, request_id=rid,
                                                 session_key=SESSION, approver=OWNER)
    assert result.success
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == OWNER_ID and kwargs["parse_mode"] == "HTML"
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert [b.text for b in buttons] == [APPROVE_LABEL, DENY_LABEL] and "Pay once" in APPROVE_LABEL
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
    # Someone else's private chat never gets the card (a group chat may: see the thread tests).
    refused = await adapter.send_payment_approval(chat_id="222", facts=_facts(), request_id=rid,
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
    # One tap = the fill and one Pay press, said in plain words with the stated total and the merchant; only a
    # matched page check covers Pay (peer r2 P2), anything else is the fill only.
    scope = {"not_checked": f"Approve: Sera fills the card only, at {origins[0]}. No total was stated",
             "matched": f"Approve: Sera fills the card and presses Pay once for 1234.50 EUR at {origins[0]}."}.get(
                 state, f"Approve: Sera fills the card only, at {origins[0]}. Hermes did not find 1234.50 EUR clearly")
    for fact in (origins[0], origins[1], "UNREVIEWED", "cc-number, cc-csc, cc-exp", "Test card",
                 PAGE_MATCH_LINES[state], "Expires in 97 s", "c" * 32, scope,
                 "one Pay press" if state == "matched" else "Pay needs your go in chat",
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


def _turn_runner(adapter, chat_id, sender=OWNER_ID, chat_type=""):
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
                                  _status_thread_metadata={"thread_id": "5"},
                                  source=SimpleNamespace(user_id=None if sender is None else str(sender),
                                                         chat_type=chat_type or ("group" if chat_id < 0 else "dm")))
    runner._approval_owner = None
    runner._schedule = schedule
    return runner


@pytest.mark.parametrize("session_chat", [GROUP, OWNER_ID, 222])
def test_card_route_and_never_the_text_fallback(monkeypatch, session_chat):
    """A group session's card goes into its own chat and thread with no notice; the owner's private chat gets it
    there; someone else's private chat gets only the neutral notice while the card goes to the owner's chat."""
    from tools import approval_context
    monkeypatch.setattr(approval_context, "payment_approver", lambda: OWNER)
    adapter = MagicMock(spec=["send_payment_approval", "retire_payment_card", "send", "send_exec_approval"])
    type(adapter).send_payment_approval = MagicMock()  # class-level, as the delivery check requires
    adapter.send_payment_approval = AsyncMock(return_value=SimpleNamespace(success=True, message_id="9"))
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    entry = _pending(exact_request=True, approver=OWNER, payment=_facts().to_dict())
    runner = _turn_runner(adapter, session_chat)
    runner._approval_notify_sync(dict(entry.data))
    call = adapter.send_payment_approval.call_args.kwargs
    assert call["approver"] == OWNER and call["request_id"] == entry.data["request_id"]
    if session_chat == 222:
        assert call["chat_id"] == str(OWNER_ID) and call["metadata"] is None
        (notice,) = adapter.send.call_args_list
        assert notice.args[:2] == ("222", GROUP_NOTICE)
    else:
        assert call["chat_id"] == str(session_chat) and call["metadata"] == {"thread_id": "5"}
        adapter.send.assert_not_called()
    adapter.send_exec_approval.assert_not_called()


@pytest.mark.parametrize("sender", [222, None, ""])
def test_a_group_card_needs_the_owner_to_have_asked(monkeypatch, sender):
    """Another member, a channel post or an anonymous admin (no user id) never puts the card in the group: it goes to
    the owner's private chat and the group gets only the neutral notice."""
    from tools import approval_context
    monkeypatch.setattr(approval_context, "payment_approver", lambda: OWNER)
    adapter = MagicMock(spec=["send_payment_approval", "retire_payment_card", "send", "send_exec_approval"])
    type(adapter).send_payment_approval = MagicMock()
    adapter.send_payment_approval = AsyncMock(return_value=SimpleNamespace(success=True, message_id="9"))
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    entry = _pending(exact_request=True, approver=OWNER, payment=_facts().to_dict())
    _turn_runner(adapter, GROUP, sender=sender)._approval_notify_sync(dict(entry.data))
    call = adapter.send_payment_approval.call_args.kwargs
    assert call["chat_id"] == str(OWNER_ID) and call["metadata"] is None
    (notice,) = adapter.send.call_args_list
    assert notice.args[:2] == (str(GROUP), GROUP_NOTICE)


@pytest.mark.parametrize("chat_type", ["channel", "dm", None])
def test_a_negative_chat_that_is_not_a_group_never_gets_the_card(monkeypatch, chat_type):
    """Peer r2 P3: a channel's direct-messages chat (or any non-group source) has a negative id too; even when the
    owner sent the turn, the card goes to his private chat and the session's chat gets only the neutral notice."""
    from tools import approval_context
    monkeypatch.setattr(approval_context, "payment_approver", lambda: OWNER)
    adapter = MagicMock(spec=["send_payment_approval", "retire_payment_card", "send", "send_exec_approval"])
    type(adapter).send_payment_approval = MagicMock()
    adapter.send_payment_approval = AsyncMock(return_value=SimpleNamespace(success=True, message_id="9"))
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    entry = _pending(exact_request=True, approver=OWNER, payment=_facts().to_dict())
    runner = _turn_runner(adapter, GROUP, chat_type="placeholder")
    runner._ctx.source.chat_type = chat_type
    runner._approval_notify_sync(dict(entry.data))
    call = adapter.send_payment_approval.call_args.kwargs
    assert call["chat_id"] == str(OWNER_ID) and call["metadata"] is None
    (notice,) = adapter.send.call_args_list
    assert notice.args[:2] == (str(GROUP), GROUP_NOTICE)


@pytest.mark.asyncio
async def test_the_adapter_refuses_a_group_card_unless_the_owner_asked():
    adapter = _adapter()
    entry = _pending(exact_request=True, approver=OWNER)
    rid = entry.data["request_id"]
    for requester in ("222", "", None):
        refused = await adapter.send_payment_approval(chat_id=str(GROUP), facts=_facts(), request_id=rid,
                                                      session_key=SESSION, approver=OWNER, requester=requester)
        assert not refused.success
    assert adapter._bot.send_message.call_count == 0
    sent = await adapter.send_payment_approval(chat_id=str(GROUP), facts=_facts(), request_id=rid,
                                               session_key=SESSION, approver=OWNER, requester=str(OWNER_ID))
    assert sent.success and adapter._bot.send_message.call_args.kwargs["chat_id"] == GROUP


class BadRequest(Exception):  # the adapter recognises Telegram's BadRequest by its class name
    pass


def _topic_gone_bot(adapter):
    """A bot that rejects any send carrying message_thread_id as a deleted topic, and posts the rest."""
    posted = adapter._bot.send_message.side_effect

    def send(**kw):
        if kw.get("message_thread_id") is not None:
            raise BadRequest("Bad Request: message thread not found")
        return posted(**kw)
    adapter._bot.send_message = AsyncMock(side_effect=send)


@pytest.mark.asyncio
async def test_a_card_without_a_stated_total_is_fill_only_in_button_and_toast():
    """Codex r1 P1-2 / peer P2: no stated total means Approve does not cover Pay: button, card and toast say so."""
    from tools.payment_consent_prompt import APPROVE_LABEL_FILL_ONLY
    adapter = _adapter()
    entry = _pending(exact_request=True, approver=OWNER)
    rid = entry.data["request_id"]
    result = await adapter.send_payment_approval(chat_id=str(OWNER_ID), facts=_facts(), request_id=rid,
                                                 session_key=SESSION, approver=OWNER)
    kwargs = adapter._bot.send_message.call_args.kwargs
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert [b.text for b in buttons] == [APPROVE_LABEL_FILL_ONLY, DENY_LABEL] and "Pay" not in APPROVE_LABEL_FILL_ONLY
    assert "does not cover pressing Pay" in kwargs["text"] and "presses Pay once" not in kwargs["text"]
    coro, query = _tap(adapter, buttons[0].callback_data, chat_id=OWNER_ID, message_id=int(result.message_id))
    await coro
    assert entry.result == "once"
    toast = query.answer.call_args.kwargs["text"]
    assert "Pay needs your go in chat" in toast and "presses Pay once" not in toast


@pytest.mark.asyncio
@pytest.mark.parametrize("state, pays", [("matched", True), ("ambiguous", False), ("not_found", False),
                                         (None, False)])
async def test_pay_once_only_when_the_page_check_matched_the_stated_total(state, pays):
    """Peer r2 P2: a stated total the page check did not match (not_found / ambiguous) is a fill-only card in its
    scope line, its footer, its button, its record and its toast, like a card with no stated total."""
    from tools.payment_consent_prompt import (APPROVE_LABEL, APPROVE_LABEL_FILL_ONLY, pays_once, render_plain,
                                              scope_line)
    facts = _facts() if state is None else _facts(cart=StatedCart((CartItem("Ticket", 1),), "12.50", "CHF"),
                                                  page_match=PageMatch(state, "the merchant page"))
    assert pays_once(facts) is pays
    text = render_plain(facts)
    assert ("presses Pay once for 12.50 CHF" in scope_line(facts)) is pays
    assert ("does not cover pressing Pay: Pay needs your go in chat" in scope_line(facts)) is not pays
    assert ("One tap covers one fill and one Pay press" in text) is pays
    assert ("One tap covers one fill only" in text) is not pays
    adapter = _adapter()
    entry = _pending(exact_request=True, approver=OWNER)
    result = await adapter.send_payment_approval(chat_id=str(OWNER_ID), facts=facts, request_id=entry.data["request_id"],
                                                 session_key=SESSION, approver=OWNER)
    buttons = [b for row in adapter._bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert buttons[0].text == (APPROVE_LABEL if pays else APPROVE_LABEL_FILL_ONLY)
    assert next(iter(adapter._approval_cards.values()))["fill_only"] is not pays
    coro, query = _tap(adapter, buttons[0].callback_data, chat_id=OWNER_ID, message_id=int(result.message_id))
    await coro
    assert ("Pay needs your go in chat" in query.answer.call_args.kwargs["text"]) is not pays


@pytest.mark.asyncio
async def test_a_card_never_retries_at_the_groups_root_when_its_topic_is_gone():
    """Codex r1 P1-1: the generic control send retries a missing topic without message_thread_id, which would post the
    card in the group's General topic. A payment card fails closed: one attempt, nothing posted, nothing armed."""
    from tools.payment_consent_prompt import CARD_TOPIC_UNAVAILABLE
    adapter = _adapter()
    _topic_gone_bot(adapter)
    rid = _pending(exact_request=True, approver=OWNER).data["request_id"]
    result = await adapter.send_payment_approval(chat_id=str(GROUP), facts=_facts(), request_id=rid, session_key=SESSION,
                                                 approver=OWNER, metadata={"thread_id": "5"}, requester=str(OWNER_ID))
    assert not result.success and result.error == CARD_TOPIC_UNAVAILABLE
    assert adapter._bot.send_message.call_count == 1  # no retry without the thread
    assert adapter._bot.send_message.call_args.kwargs["message_thread_id"] == 5
    assert not adapter._approval_cards


def test_a_gone_topic_sends_the_card_to_the_owners_chat_and_a_notice_to_the_session(monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "payment_approver", lambda: OWNER)
    adapter = _adapter()
    _topic_gone_bot(adapter)
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    entry = _pending(exact_request=True, approver=OWNER, payment=_facts().to_dict())
    _turn_runner(adapter, GROUP)._approval_notify_sync(dict(entry.data))
    calls = adapter._bot.send_message.call_args_list
    assert [(c.kwargs["chat_id"], c.kwargs.get("message_thread_id")) for c in calls] == [(GROUP, 5), (OWNER_ID, None)]
    assert len(adapter._approval_cards) == 1 and next(iter(adapter._approval_cards.values()))["chat_id"] == str(OWNER_ID)
    (notice,) = adapter.send.call_args_list
    assert notice.args[:2] == (str(GROUP), GROUP_NOTICE)


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


def test_a_card_that_posts_after_the_delivery_timeout_is_retired(monkeypatch):
    """Codex A1 review P3: the wait gives up after SEND_TIMEOUT_S, but Telegram may still post the
    card later. That late card must lose its buttons, not look live for a withdrawn request."""
    import concurrent.futures
    from gateway import payment_approval
    from tools import approval_context
    monkeypatch.setattr(approval_context, "payment_approver", lambda: OWNER)
    monkeypatch.setattr(payment_approval, "SEND_TIMEOUT_S", 0.01)

    class Card:
        send = AsyncMock()
        send_exec_approval = AsyncMock()
        send_payment_approval = AsyncMock(return_value=SimpleNamespace(success=True, message_id="9"))
        retire_payment_card = AsyncMock()

    scheduled, send = [], concurrent.futures.Future()

    def schedule(coro, _msg, loop=None):
        scheduled.append(coro)
        coro.close()  # the adapter calls are AsyncMocks: only the scheduling matters here
        return send if len(scheduled) == 1 else concurrent.futures.Future()

    runner = _turn_runner(Card(), OWNER_ID)
    runner._schedule = schedule
    data = dict(_pending(exact_request=True, approver=OWNER, payment=_facts().to_dict()).data)
    with pytest.raises(payment_approval.PaymentCardUndeliverable):
        payment_approval.deliver(runner, data)
    assert len(scheduled) == 1
    send.set_result(SimpleNamespace(success=True, message_id="9"))  # the card posts late
    assert len(scheduled) == 2
    Card.retire_payment_card.assert_called_once_with(data["request_id"], "unavailable")
