"""dlz: deliver a card-fill approval as the payment card where the owner talks to Sera.

Called by ``TurnRunner._render_approval_sync`` for approval data carrying ``"payment"`` (built by
``tools.approval_prompt._elicit_payment``). Every failure raises, which the gateway wait turns into
``notify_failed`` and so an ``unavailable`` consent: there is no plain-text ``/approve`` fallback and
nothing is ever shown shortened.

Where the card goes (owner decision 2026-11-13): a group session (a negative Telegram chat id, e.g. the
forum topic he talks to Sera in) gets the card in its own chat and thread only when the OWNER sent the turn;
the owner's own private chat gets it there, in the session's thread. Only the owner's own Telegram user can act on it either way (the
adapter's presser check); anyone else's tap changes nothing. Every other case (another member's turn, a channel post, an
anonymous admin, someone else's private chat) sends the card to the owner's private chat as before and the
session's chat gets a fixed notice with no details.
One reminder replying to the card goes to the same place ``REMINDER_AT_S`` after the send, only while the
card is still unanswered; the expiry notice goes there too.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

SEND_TIMEOUT_S = 15
REMINDER_AT_S = 120
# No reminder when less than this would be left at REMINDER_AT_S (a short window needs none).
REMINDER_MIN_LEFT_S = 60
# dlz: the owner's one plain message when a card expires unanswered; never any card or cart detail.
EXPIRED_NOTICE = "Card approval expired: nothing was filled or paid. Ask Sera to retry if you still want it."
EXPIRED_NOTICE_DEGRADED = ("Card approval expired while Hermes' Telegram connection was reconnecting, so a tap may "
                           "not have arrived. Nothing was filled or paid. Ask Sera to retry if you still want it.")


class PaymentCardUndeliverable(RuntimeError):
    """The payment card was not (or not certainly) delivered; the prompt is unavailable."""


def _approver_user_id(approver) -> str:
    platform, _, user_id = str(approver or "").partition(":")
    if platform != "telegram" or not user_id.isascii() or not user_id.isdigit():
        raise PaymentCardUndeliverable("no payment approver")
    return user_id


def _retire_if_sent_late(turn_runner, adapter, fut, request_id: str) -> None:
    """The prompt is already unavailable (the wait withdraws it). The send is not cancelled, since a
    cancelled in-flight send could still post a card that was never armed; instead a card that posts
    after this (Telegram's own deadline is longer than ``SEND_TIMEOUT_S``) is retired at once, so no
    card with live-looking buttons outlives its request."""
    def done(f) -> None:
        if f.cancelled() or f.exception() is not None or not getattr(f.result(), "success", False):
            return
        # The card did post after the wait gave up: trace it (ids only) so "did it reach the owner?" has an answer.
        logger.info("Payment approval card posted late and is withdrawn (request_id=%s, message_id=%s)",
                    request_id, getattr(f.result(), "message_id", None))
        turn_runner._schedule(adapter.retire_payment_card(request_id, "unavailable"),
                              "late payment card retire scheduling error")

    fut.add_done_callback(done)


def _delivery_probe(adapter):
    """The adapter's "was my transport disrupted since the card went out" check, armed now; None when the
    adapter has none (only Telegram sends payment cards, so in practice every payment card has one)."""
    if not callable(getattr(type(adapter), "payment_delivery_disrupted", None)):
        return None
    mark = adapter.payment_delivery_mark()
    verdict: list = []

    def probe() -> bool:
        # Asked only after the wait ended (the expiry notice, then the outcome): one answer for both.
        if not verdict:
            try:
                verdict.append(bool(adapter.payment_delivery_disrupted(mark)))
            except Exception:  # health: allow BLE001 -- a check that cannot answer counts as disrupted (never an approval)
                logger.debug("payment delivery check failed", exc_info=True)
                verdict.append(True)
        return verdict[0]
    return probe


def _notify_expired(turn_runner, adapter, chat_id: str, metadata, probe) -> None:
    """Best effort: one plain message where the card is that it expired (a card edit alone does not notify)."""
    from gateway.run import _interim_metadata

    text = EXPIRED_NOTICE_DEGRADED if probe is not None and probe() else EXPIRED_NOTICE
    turn_runner._schedule(adapter.send(chat_id, text, metadata=_interim_metadata(metadata)),
                          "payment expiry notice scheduling error")


def reminder_text(left_s: float) -> str:
    """The one reminder: time left only, never any card or cart detail."""
    return f"Card approval waiting — {max(1, round(left_s / 60))} min left. Tap Approve on the card above."


async def _wait(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _remind_later(adapter, request_id: str, text: str, state: dict) -> None:
    """After REMINDER_AT_S: one reply to the card, only while it is unanswered. A settle cancels this task and
    sets ``state["settled"]``; the adapter also sends nothing once the card's record is gone (tapped or retired)."""
    await _wait(REMINDER_AT_S)
    if state["settled"]:
        return
    sent = await adapter.remind_payment_card(request_id, text)
    logger.info("Payment approval reminder (request_id=%s, sent=%s)", request_id, bool(sent))


def _requester_id(ctx) -> str:
    """The Telegram user id of whoever sent the turn (empty for a channel post or an anonymous admin)."""
    return str(getattr(getattr(ctx, "source", None), "user_id", "") or "")


def _card_route(ctx, user_id: str) -> tuple[str, object, bool]:
    """``(chat_id, thread metadata, other_private_chat)`` for the card: the session's own chat and thread when it
    is the owner's private chat, or a group AND the owner himself asked in it; every other case (another member,
    a channel post, an anonymous admin) is the owner's private chat plus a notice in the session's chat."""
    chat = str(ctx._status_chat_id or "")
    group = chat.startswith("-") and chat[1:].isascii() and chat[1:].isdigit()
    if chat == user_id or (group and _requester_id(ctx) == user_id):
        return chat, ctx._status_thread_metadata, False
    return user_id, None, True


def deliver(turn_runner, approval_data: dict) -> None:
    """Send the card through the session's own Telegram adapter (see ``_card_route``), arm the card's
    retirement for when the prompt settles, and arm the one reminder."""
    from tools.approval import attach_delivery_probe, observe_gateway_settle, valid_request_id
    from tools.approval_context import payment_approver
    from tools.payment_consent_prompt import CARD_TOPIC_UNAVAILABLE, GROUP_NOTICE, PaymentPromptFacts

    ctx = turn_runner._ctx
    adapter = ctx._status_adapter
    session_key = ctx.session_key or ""
    request_id = valid_request_id(approval_data.get("request_id"))
    approver = approval_data.get("approver")
    if request_id is None or not session_key or not approval_data.get("exact_request"):
        raise PaymentCardUndeliverable("payment prompt without an exact request id")
    # The configured owner must still be the one this prompt was bound to.
    if approver != payment_approver():
        raise PaymentCardUndeliverable("payment approver changed or unset")
    user_id = _approver_user_id(approver)
    facts = PaymentPromptFacts.from_dict(approval_data.get("payment"))
    # Class check, not instance: MagicMock adapters invent every attribute.
    if not callable(getattr(type(adapter), "send_payment_approval", None)):
        raise PaymentCardUndeliverable("this session's adapter cannot send the payment card")
    route = _card_route(ctx, user_id)
    probe = _delivery_probe(adapter)  # armed before the send: a reconnect while it is in flight counts too
    result = None
    for attempt in range(2):
        chat_id, metadata, other_private_chat = route
        fut = turn_runner._schedule(
            adapter.send_payment_approval(chat_id=chat_id, facts=facts, request_id=request_id,
                                          session_key=session_key, approver=approver, metadata=metadata,
                                          requester=_requester_id(ctx)),
            "send_payment_approval scheduling error")
        if fut is None:
            raise PaymentCardUndeliverable("gateway loop unavailable")
        try:
            result = fut.result(timeout=SEND_TIMEOUT_S)
        except Exception as exc:  # timeout included: a card that may have posted is still unavailable
            _retire_if_sent_late(turn_runner, adapter, fut, request_id)
            raise PaymentCardUndeliverable(f"payment card send failed: {type(exc).__name__}") from None
        # The session's topic is gone: the adapter sent nothing (never the group's General topic); the card goes
        # to the owner's own chat once, with the neutral notice in the session's chat.
        if (attempt == 0 and not other_private_chat and not getattr(result, "success", False)
                and getattr(result, "error", None) == CARD_TOPIC_UNAVAILABLE):
            logger.warning("Payment approval card topic unavailable (request_id=%s); sending to the owner's chat", request_id)
            route = (user_id, None, True)
            continue
        break
    if not getattr(result, "success", False):
        raise PaymentCardUndeliverable("payment card send failed")
    # Delivery trace: correlation and timing only, never card, cart or amount text.
    window_s = approval_data.get("timeout_s") or facts.expires_in_s
    logger.info("Payment approval card sent (request_id=%s, chat=%s, message_id=%s, expires_in_s=%s)",
                request_id, chat_id, getattr(result, "message_id", None), window_s)
    # Settles run on this (the waiting) thread after deliver returns, or inline just below: never concurrently.
    reminder: dict = {"settled": False, "future": None}

    def retire(reason: str) -> None:
        reminder["settled"] = True
        if reminder["future"] is not None:
            reminder["future"].cancel()
        turn_runner._schedule(adapter.retire_payment_card(request_id, reason), "payment card retire scheduling error")
        if reason == "timeout":
            _notify_expired(turn_runner, adapter, chat_id, metadata, probe)

    if probe is not None:
        attach_delivery_probe(session_key, request_id, probe)
    if not observe_gateway_settle(session_key, request_id, retire):
        # Settled while the card was being sent: retire it now.
        retire("resolved")
    left_s = int(window_s) - REMINDER_AT_S
    if (not reminder["settled"] and left_s >= REMINDER_MIN_LEFT_S
            and callable(getattr(type(adapter), "remind_payment_card", None))):
        reminder["future"] = turn_runner._schedule(
            _remind_later(adapter, request_id, reminder_text(left_s), reminder), "payment reminder scheduling error")
    if other_private_chat:
        try:
            from gateway.run import _interim_metadata
            turn_runner._schedule(
                adapter.send(ctx._status_chat_id, GROUP_NOTICE, metadata=_interim_metadata(ctx._status_thread_metadata)),
                "payment notice scheduling error")
        except Exception:  # health: allow BLE001 -- the owner has the card; the notice is best effort
            logger.debug("payment approval group notice failed", exc_info=True)
