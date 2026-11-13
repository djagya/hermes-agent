"""The card-fill approval where the owner talks to Sera: same thread, a 5-minute window, one reminder.

Owner decisions 2026-11-13 (four approvals expired untapped the night before: the card sat in another chat):
- a group session's card goes into that chat and thread; only the owner's own Telegram user can act on it, and
  anyone else's tap (Approve or Deny, allowlisted or not) changes nothing;
- the window is 5 minutes, with one reminder replying to the card at 2 minutes while it is unanswered, never after
  it settled;
- the card says one tap is the fill and one Pay press.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Module level on purpose (see test_approval_delivery_observability): gateway.run's import side effects
# must happen at collection time, outside the per-test home-I/O guard.
import gateway.run
from gateway import payment_approval
from gateway.config import PlatformConfig
from tools import approval
from tools.payment_consent_prompt import (
    CartItem, PageMatch, PaymentPromptFacts, ReceivingOrigin, StatedCart, request_payment_approval)
from tests.waiting import wait_until

OWNER_ID = 111
OWNER = f"telegram:{OWNER_ID}"
GROUP = -1001234
THREAD = 5
SESSION = f"agent:main:telegram:group:{GROUP}:{THREAD}"
LABEL = "Distinct Card Label"
AMOUNT = "4321.98"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from tools import approval_context
    button = lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data)
    markup = lambda rows: SimpleNamespace(inline_keyboard=rows)
    monkeypatch.setattr(approval_context, "payment_approver", lambda: OWNER)
    monkeypatch.setattr(approval_context, "get_current_session_key", lambda default="default": SESSION)
    monkeypatch.setattr(approval_context, "_no_user_can_answer", lambda: False)
    monkeypatch.setattr(approval_context, "_is_gateway_approval_context", lambda: True)
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": f"{OWNER_ID},222"}), \
            patch("plugins.platforms.telegram.adapter.InlineKeyboardButton", button), \
            patch("plugins.platforms.telegram.adapter.InlineKeyboardMarkup", markup):
        yield
    with approval._lock:
        approval._gateway_queues.pop(SESSION, None)
        approval._gateway_notify_cbs.pop(SESSION, None)


@pytest.fixture
def loop():
    """The gateway loop, on its own thread as in production."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def _facts(expires_in_s=60):
    return PaymentPromptFacts(
        ref="a" * 32, card_label=LABEL, merchant_origin="https://shop.example",
        receiving=(ReceivingOrigin("https://shop.example", "merchant", ("cc-number", "cc-csc")),),
        expires_in_s=expires_in_s, cart=StatedCart((CartItem("Zurich ticket", 1),), AMOUNT, "CHF"),
        page_match=PageMatch("matched", "https://shop.example"))


def _adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t"))
    adapter._bot, adapter._app = AsyncMock(), MagicMock()
    ids = iter(range(40, 100))
    adapter._bot.send_message = AsyncMock(side_effect=lambda **kw: SimpleNamespace(
        message_id=next(ids), chat_id=kw["chat_id"], message_thread_id=kw.get("message_thread_id")))
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))  # plain notices
    return adapter


def _runner(adapter, loop, scheduled):
    from gateway.run_turn_runner import TurnRunner

    def schedule(coro, _msg, loop_=None):
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        scheduled.append((coro.cr_code.co_name, fut))
        return fut

    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(_status_adapter=adapter, _status_chat_id=str(GROUP), session_key=SESSION,
                                  _status_thread_metadata={"thread_id": str(THREAD)},
                                  source=SimpleNamespace(user_id=str(OWNER_ID)))
    runner._approval_owner = None
    runner._schedule = schedule
    approval.register_gateway_notify(SESSION, runner._approval_notify_sync)
    return runner


def _tap(adapter, loop, data, message_id, user_id):
    query = AsyncMock()
    query.data = data
    query.message = SimpleNamespace(chat_id=GROUP, message_id=message_id, message_thread_id=THREAD,
                                    chat=SimpleNamespace(type="supergroup"), text_html="card")
    query.from_user = SimpleNamespace(id=user_id, first_name="U")
    coro = adapter._handle_callback_query(SimpleNamespace(callback_query=query), MagicMock())
    asyncio.run_coroutine_threadsafe(coro, loop).result(10)
    return query


def _ask(result, facts, timeout_s):
    worker = threading.Thread(target=lambda: result.update(
        consent=request_payment_approval(facts, timeout_seconds=timeout_s)))
    worker.start()
    return worker


def _card(adapter):
    kwargs = adapter._bot.send_message.await_args_list[0].kwargs
    buttons = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    return kwargs, buttons


def test_a_group_card_goes_to_its_thread_and_only_the_owners_tap_acts(loop):
    adapter, scheduled, result = _adapter(), [], {}
    _runner(adapter, loop, scheduled)
    worker = _ask(result, _facts(), 30)
    assert wait_until(lambda: adapter._bot.send_message.await_count == 1 and adapter._approval_cards,
                      what="card sent and armed")
    kwargs, (approve, deny) = _card(adapter)
    assert kwargs["chat_id"] == GROUP and kwargs["message_thread_id"] == THREAD
    assert "presses Pay once for 4321.98 CHF at https://shop.example" in kwargs["text"]
    adapter.send.assert_not_called()  # no neutral notice: the card is in the thread
    # Another member taps (allowlisted or not, Approve or Deny): answered, nothing resolved, the card stays live.
    for data, user in ((approve, 222), (deny, 222), (approve, 333), (deny, 333)):
        query = _tap(adapter, loop, data, 40, user)
        assert "Only the owner can approve" in query.answer.await_args.kwargs["text"]
    assert approval.has_blocking_approval(SESSION) and "consent" not in result
    assert len(adapter._approval_cards) == 1
    _tap(adapter, loop, approve, 40, OWNER_ID)
    worker.join(10)
    assert result["consent"].outcome == "accept"
    assert [name for name, _ in scheduled].count("_remind_later") == 0  # a 30 s window needs no reminder


def test_one_reminder_replies_to_the_card_and_the_expiry_notice_lands_in_the_thread(loop, monkeypatch):
    monkeypatch.setattr(payment_approval, "REMINDER_AT_S", 0.05)
    monkeypatch.setattr(payment_approval, "REMINDER_MIN_LEFT_S", 0)
    adapter, scheduled, result = _adapter(), [], {}
    _runner(adapter, loop, scheduled)
    worker = _ask(result, _facts(), 2)
    assert wait_until(lambda: adapter._bot.send_message.await_count == 2, what="reminder sent")
    reminder = adapter._bot.send_message.await_args_list[1].kwargs
    assert (reminder["chat_id"], reminder["message_thread_id"], reminder["reply_to_message_id"]) == (GROUP, THREAD, 40)
    assert "min left" in reminder["text"] and LABEL not in reminder["text"] and AMOUNT not in reminder["text"]
    worker.join(10)
    assert result["consent"].outcome == "timeout"
    assert wait_until(lambda: adapter.send.await_count == 1, what="expiry notice")
    notice = adapter.send.await_args
    assert notice.args == (str(GROUP), payment_approval.EXPIRED_NOTICE)
    assert notice.kwargs["metadata"]["thread_id"] == str(THREAD)
    assert adapter._bot.send_message.await_count == 2  # one reminder, never a second


@pytest.mark.parametrize("settle", ["owner_approves", "typed_deny"])
def test_no_reminder_once_the_card_settled(loop, monkeypatch, settle):
    gate = threading.Event()

    async def held(_seconds):  # the reminder's timer runs out only when the test says so
        await asyncio.to_thread(gate.wait, 10)

    monkeypatch.setattr(payment_approval, "_wait", held)
    adapter, scheduled, result = _adapter(), [], {}
    _runner(adapter, loop, scheduled)
    worker = _ask(result, _facts(300), 300)
    assert wait_until(lambda: [n for n, _ in scheduled].count("_remind_later") == 1, what="reminder armed")
    _, (approve, _deny) = _card(adapter)
    rid = next(r["request_id"] for r in adapter._approval_cards.values())
    if settle == "owner_approves":
        _tap(adapter, loop, approve, 40, OWNER_ID)
    else:
        assert approval.resolve_gateway_approval(SESSION, "deny", request_id=rid) == 1
    worker.join(10)
    assert result["consent"].outcome == ("accept" if settle == "owner_approves" else "deny")
    gate.set()
    (fut,) = [f for n, f in scheduled if n == "_remind_later"]
    assert wait_until(fut.done, what="reminder task ended")
    assert wait_until(lambda: not adapter._approval_cards, what="card retired")
    # Even a reminder that outlived its cancellation finds no armed card and sends nothing.
    assert asyncio.run_coroutine_threadsafe(adapter.remind_payment_card(rid, "x"), loop).result(10) is False
    assert adapter._bot.send_message.await_count == 1  # the card only


def test_a_five_minute_window_is_not_cut_and_reminds_at_two_minutes_with_three_left(loop, monkeypatch):
    from agent.vault_login_classifier import LoginControl, classify_checkout_control
    from tools.browser_supervisor_payment import INSPECTION_WINDOW_S, PaymentDocument, PaymentInspection
    from tools.payment_consent_prompt import facts_for_inspection

    assert INSPECTION_WINDOW_S == 300  # owner decision 2026-11-13 (was 120 s)
    merchant = "https://shop.example"
    controls = (classify_checkout_control(LoginControl.from_dict({"index": 0, "autocomplete": "cc-number"})),)
    inspection = PaymentInspection("a" * 32, "page", "target", merchant,
                                   (PaymentDocument("f0", "s0", "o0", merchant, controls),), (),
                                   time.monotonic() + INSPECTION_WINDOW_S)
    assert 295 <= facts_for_inspection(inspection, card_label=LABEL).expires_in_s <= 300
    gate, waited, texts = threading.Event(), [], []

    async def held(seconds):
        waited.append(seconds)
        await asyncio.to_thread(gate.wait, 10)

    real_remind = payment_approval._remind_later

    async def spy(adapter, request_id, text, state):
        texts.append(text)
        await real_remind(adapter, request_id, text, state)

    monkeypatch.setattr(payment_approval, "_wait", held)
    monkeypatch.setattr(payment_approval, "_remind_later", spy)
    adapter, scheduled, result = _adapter(), [], {}
    _runner(adapter, loop, scheduled)
    worker = _ask(result, _facts(300), 300)
    assert wait_until(lambda: waited, what="reminder armed")
    with approval._lock:
        (entry,) = approval._gateway_queues[SESSION]
    assert entry.data["timeout_s"] == 300  # the default approvals.timeout does not cut it
    assert "Expires in 300 s" in adapter._bot.send_message.await_args_list[0].kwargs["text"]
    assert waited == [120] and texts == ["Card approval waiting — 3 min left. Tap Approve on the card above."]
    _, (approve, _deny) = _card(adapter)
    _tap(adapter, loop, approve, 40, OWNER_ID)
    worker.join(10)
    gate.set()
    assert result["consent"].outcome == "accept"


@pytest.mark.parametrize("configured, reminders, stated", [(120, 0, 120), (150, 0, 150), (200, 1, 200)])
def test_the_card_states_the_effective_window_and_a_short_one_gets_no_reminder(loop, monkeypatch, configured, reminders,
                                                                                stated):
    """peer r1 P3 / GLM P3-2: with approvals.timeout below the inspection window the card, the retire time and the
    reminder all use the smaller one; an effective window of 150 s or less needs no reminder."""
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: configured)
    gate = threading.Event()

    async def held(_seconds):
        await asyncio.to_thread(gate.wait, 10)

    monkeypatch.setattr(payment_approval, "_wait", held)
    adapter, scheduled, result = _adapter(), [], {}
    _runner(adapter, loop, scheduled)
    worker = _ask(result, _facts(300), 300)  # the inspection has 300 s left; the config allows less
    assert wait_until(lambda: adapter._bot.send_message.await_count == 1 and adapter._approval_cards,
                      what="card sent and armed")
    with approval._lock:
        (entry,) = approval._gateway_queues[SESSION]
    assert entry.data["timeout_s"] == stated
    assert f"Expires in {stated} s" in adapter._bot.send_message.await_args_list[0].kwargs["text"]
    assert [name for name, _ in scheduled].count("_remind_later") == reminders
    _, (approve, _deny) = _card(adapter)
    _tap(adapter, loop, approve, 40, OWNER_ID)
    worker.join(10)
    gate.set()
    assert result["consent"].outcome == "accept"


def test_a_reminder_that_dies_is_logged_and_never_raises(loop, caplog):
    """GLM r1 P3-3: the reminder's future is never read, so its body must not let an error vanish unlogged."""
    import logging
    caplog.set_level(logging.DEBUG, logger="gateway.payment_approval")
    adapter = MagicMock()
    adapter.remind_payment_card = AsyncMock(side_effect=RuntimeError("boom"))

    async def go():
        with patch.object(payment_approval, "REMINDER_AT_S", 0):
            await payment_approval._remind_later(adapter, "r" * 32, "text", {"settled": False})

    asyncio.run_coroutine_threadsafe(go(), loop).result(5)  # returns: nothing escapes
    assert adapter.remind_payment_card.await_count == 1
    assert any("reminder failed" in r.getMessage() for r in caplog.records)
