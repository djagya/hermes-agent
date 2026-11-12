"""The card-fill approval leaves a record at every boundary and never reports a lost card as "nobody answered".

Incident R-op-2026-11-12-1: a card-fill approval ran its full wait to ``consent_outcome=timeout`` while the owner
was active in the chat, Telegram polling reconnected twice during the wait, and no log line showed whether the card
was sent or a tap arrived. The card's tap record lives only on the adapter that sent it, so a tap that reaches a
rebuilt adapter, or arrives after a long reconnect, resolves nothing.

These tests pin: a send line and a callback line (correlation only, never card or cart text); a failed send ends
the wait at once as ``unavailable``; an unanswered card whose Telegram connection was disrupted is ``unavailable``,
not ``timeout``; an expired card sends the owner one plain message.
"""

from __future__ import annotations

import logging
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
from tools.approval_gateway_wait import _ApprovalEntry
from tools.payment_consent_prompt import (
    UNAVAILABLE_DEGRADED, UNAVAILABLE_UNDELIVERED, CartItem, PageMatch, PaymentPromptFacts, ReceivingOrigin,
    StatedCart, request_payment_approval)
from tests.waiting import wait_until

OWNER_ID = 111
OWNER = f"telegram:{OWNER_ID}"
SESSION = f"agent:main:telegram:dm:{OWNER_ID}"
LABEL = "Distinct Card Label"
AMOUNT = "4321.98"
ITEM = "Zurich ticket"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from tools import approval_context
    button = lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data)
    markup = lambda rows: SimpleNamespace(inline_keyboard=rows)
    monkeypatch.setattr(approval_context, "payment_approver", lambda: OWNER)
    monkeypatch.setattr(approval_context, "get_current_session_key", lambda default="default": SESSION)
    monkeypatch.setattr(approval_context, "_no_user_can_answer", lambda: False)
    monkeypatch.setattr(approval_context, "_is_gateway_approval_context", lambda: True)
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": str(OWNER_ID)}), \
            patch("plugins.platforms.telegram.adapter.InlineKeyboardButton", button), \
            patch("plugins.platforms.telegram.adapter.InlineKeyboardMarkup", markup):
        yield
    with approval._lock:
        approval._gateway_queues.pop(SESSION, None)
        approval._gateway_notify_cbs.pop(SESSION, None)


def _facts():
    return PaymentPromptFacts(
        ref="a" * 32, card_label=LABEL, merchant_origin="https://shop.example",
        receiving=(ReceivingOrigin("https://shop.example", "merchant", ("cc-number", "cc-csc")),),
        expires_in_s=90, cart=StatedCart((CartItem(ITEM, 1),), AMOUNT, "CHF"),
        page_match=PageMatch("matched", "https://shop.example"))


def _adapter(*, send_ok=True):
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t"))
    adapter._bot, adapter._app = AsyncMock(), MagicMock()
    ids = iter(range(40, 100))

    def send_message(**kw):
        if not send_ok:
            raise RuntimeError("Bad Gateway")
        return SimpleNamespace(message_id=next(ids), chat_id=kw["chat_id"], message_thread_id=None)

    adapter._bot.send_message = AsyncMock(side_effect=send_message)
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))  # the owner's plain messages
    return adapter


def _runner(adapter):
    """A TurnRunner whose gateway loop is ``asyncio.run`` on the calling thread."""
    import asyncio
    import concurrent.futures
    from gateway.run_turn_runner import TurnRunner

    def schedule(coro, _msg, loop=None):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(asyncio.run(coro))
        except Exception as exc:
            fut.set_exception(exc)
        return fut

    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(_status_adapter=adapter, _status_chat_id=str(OWNER_ID), session_key=SESSION,
                                  _status_thread_metadata=None)
    runner._approval_owner = None
    runner._schedule = schedule
    approval.register_gateway_notify(SESSION, runner._approval_notify_sync)
    return runner


def _tap(adapter, data, message_id):
    query = AsyncMock()
    query.data = data
    query.message = SimpleNamespace(chat_id=OWNER_ID, message_id=message_id, message_thread_id=None,
                                    chat=SimpleNamespace(type="private"), text_html="card")
    query.from_user = SimpleNamespace(id=OWNER_ID, first_name="U")
    return adapter._handle_callback_query(SimpleNamespace(callback_query=query), MagicMock())


def _ask(timeout_seconds):
    started = time.monotonic()
    consent = request_payment_approval(_facts(), timeout_seconds=timeout_seconds)
    return consent, time.monotonic() - started


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


def _assert_no_card_text(caplog, adapter):
    logged = " ".join(_messages(caplog))
    owner_messages = " ".join(str(c.args) for c in adapter.send.call_args_list)
    for secret in (LABEL, AMOUNT, ITEM):
        assert secret not in logged and secret not in owner_messages


def test_an_answered_card_logs_its_send_and_its_callback(caplog):
    caplog.set_level(logging.INFO)
    adapter = _adapter()
    _runner(adapter)
    result = {}
    worker = threading.Thread(target=lambda: result.update(consent=_ask(30)[0]))
    worker.start()
    assert wait_until(lambda: adapter._bot.send_message.call_count == 1, what="card sent")
    kwargs = adapter._bot.send_message.call_args.kwargs
    approve = kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    import asyncio
    asyncio.run(_tap(adapter, approve, 40))
    worker.join(10)
    assert result["consent"].outcome == "accept"
    messages = _messages(caplog)
    (sent,) = [m for m in messages if m.startswith("Payment approval card sent")]
    assert f"chat={OWNER_ID}" in sent and "message_id=40" in sent and "expires_in_s=30" in sent
    (callback,) = [m for m in messages if "Payment approval callback" in m]
    assert "message_id=40" in callback and "choice=once" in callback and "applied=True" in callback
    adapter.send.assert_not_called()  # answered: no expiry message
    _assert_no_card_text(caplog, adapter)


def test_a_failed_card_send_ends_the_wait_at_once_as_unavailable(caplog):
    caplog.set_level(logging.INFO)
    adapter = _adapter(send_ok=False)
    _runner(adapter)
    consent, elapsed = _ask(30)
    assert (consent.outcome, consent.reason, consent.detail) == ("unavailable", "unavailable", UNAVAILABLE_UNDELIVERED)
    assert elapsed < 5 and not approval.has_blocking_approval(SESSION)
    assert not [m for m in _messages(caplog) if m.startswith("Payment approval card sent")]
    adapter.send.assert_not_called()
    _assert_no_card_text(caplog, adapter)


def test_an_unanswered_card_times_out_and_tells_the_owner_once(caplog):
    caplog.set_level(logging.INFO)
    adapter = _adapter()
    _runner(adapter)
    consent, _ = _ask(1)
    assert (consent.outcome, consent.reason) == ("timeout", "timeout")
    adapter.send.assert_called_once_with(str(OWNER_ID), payment_approval.EXPIRED_NOTICE)
    adapter._bot.edit_message_text.assert_called_once()  # the card itself loses its buttons
    _assert_no_card_text(caplog, adapter)


@pytest.mark.parametrize("disruption", ["reconnect", "still_degraded", "adapter_rebuilt"])
def test_an_unanswered_card_whose_connection_dropped_is_unavailable_not_timeout(caplog, disruption):
    caplog.set_level(logging.INFO)
    adapter = _adapter()
    _runner(adapter)

    def disrupt():
        wait_until(lambda: adapter._bot.send_message.call_count == 1, what="card sent")
        if disruption == "reconnect":  # polling restarted in place; it is healthy again by the deadline
            adapter._polling_generation += 1
        elif disruption == "still_degraded":
            adapter._send_path_degraded = True
        else:  # the supervisor rebuilt the adapter; the turn still holds this one
            live = _adapter()
            adapter.gateway_runner = SimpleNamespace(_adapters_for_profile=lambda profile: {adapter.platform: live})

    thread = threading.Thread(target=disrupt)
    thread.start()
    consent, _ = _ask(1)
    thread.join(5)
    assert (consent.outcome, consent.reason, consent.detail) == ("unavailable", "unavailable", UNAVAILABLE_DEGRADED)
    adapter.send.assert_called_once_with(str(OWNER_ID), payment_approval.EXPIRED_NOTICE_DEGRADED)
    assert any("delivery_degraded=True" in m for m in _messages(caplog) if m.startswith("Approval wait ended"))
    _assert_no_card_text(caplog, adapter)


def test_a_tap_that_reaches_a_rebuilt_adapter_resolves_nothing_and_is_logged(caplog):
    """The lost-callback path: the card's record lives on the adapter that sent it."""
    import asyncio
    caplog.set_level(logging.INFO)
    entry = _ApprovalEntry({"command": "fill", "exact_request": True, "approver": OWNER})
    with approval._lock:
        approval._gateway_queues.setdefault(SESSION, []).append(entry)
    sender = _adapter()
    sent = asyncio.run(sender.send_payment_approval(chat_id=str(OWNER_ID), facts=_facts(),
                                                    request_id=entry.data["request_id"], session_key=SESSION,
                                                    approver=OWNER))
    approve = sender._bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    rebuilt = _adapter()
    asyncio.run(_tap(rebuilt, approve, int(sent.message_id)))
    assert entry.result is None
    (refused,) = [m for m in _messages(caplog) if "Payment approval tap refused" in m]
    assert "no armed card on this adapter" in refused and approve.split(":")[2] not in refused


def test_a_degraded_wait_reaches_the_model_as_consent_missing_with_nothing_read(monkeypatch):
    """Through the tool: the model gets payment_consent_missing / unavailable, no card value is resolved,
    and the result carries the plain next step."""
    import json

    from agent.vault_login_classifier import LoginControl, classify_checkout_control
    from agent.vault_payment_policy import PAYMENT_PSP_ORIGINS
    from tools import browser_vault_payment as bvp
    from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection

    class Notifier:
        renders_payment_cards = True

        def __call__(self, data):  # the card went out, then the connection dropped before any tap
            approval.attach_delivery_probe(SESSION, data["request_id"], lambda: True)

    approval.register_gateway_notify(SESSION, Notifier())
    merchant = "https://shop.example"
    controls = tuple(classify_checkout_control(LoginControl.from_dict({"index": n, "autocomplete": t}))
                     for n, t in enumerate(("cc-number", "cc-csc")))
    inspection = PaymentInspection("e" * 32, "page", "target", merchant,
                                   (PaymentDocument("f0", "s0", "o0", merchant, controls),), (), time.monotonic() + 2)
    supervisor = SimpleNamespace(inspect_payment=lambda *a, **k: inspection, discard_payment=lambda i: None,
                                 payment_is_current=lambda i: True)
    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: supervisor)
    meta = SimpleNamespace(id="h", label=LABEL, allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)
    resolved = []
    out = json.loads(bvp.fill_payment(SimpleNamespace(name="local", resolve_secret=resolved.append), meta, "task"))
    assert out["error_type"] == "payment_consent_missing" and out["consent_outcome"] == "unavailable"
    assert UNAVAILABLE_DEGRADED in out["error"] and "do not retry the fill on your own" in out["error"]
    assert not resolved
