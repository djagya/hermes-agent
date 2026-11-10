"""dlz: deliver a card-fill approval as the payment card in the owner's private Telegram chat.

Called by ``TurnRunner._render_approval_sync`` for approval data carrying ``"payment"`` (built by
``tools.approval_prompt._elicit_payment``). Every failure raises, which the gateway wait turns into
``notify_failed`` and so an ``unavailable`` consent: there is no plain-text ``/approve`` fallback and
nothing is ever shown shortened. The session's own chat, when it is not that private chat, gets a
fixed notice with no details.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SEND_TIMEOUT_S = 15


class PaymentCardUndeliverable(RuntimeError):
    """The payment card was not (or not certainly) delivered; the prompt is unavailable."""


def _approver_user_id(approver) -> str:
    platform, _, user_id = str(approver or "").partition(":")
    if platform != "telegram" or not user_id.isascii() or not user_id.isdigit():
        raise PaymentCardUndeliverable("no payment approver")
    return user_id


def deliver(turn_runner, approval_data: dict) -> None:
    """Send the card through the session's own Telegram adapter to the approver's private chat,
    arm the card's retirement for when the prompt settles, and post the neutral notice to a group."""
    from tools.approval import observe_gateway_settle, valid_request_id
    from tools.approval_context import payment_approver
    from tools.payment_consent_prompt import GROUP_NOTICE, PaymentPromptFacts

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
    fut = turn_runner._schedule(
        adapter.send_payment_approval(chat_id=user_id, facts=facts, request_id=request_id,
                                      session_key=session_key, approver=approver),
        "send_payment_approval scheduling error")
    if fut is None:
        raise PaymentCardUndeliverable("gateway loop unavailable")
    try:
        result = fut.result(timeout=SEND_TIMEOUT_S)
    except Exception as exc:  # timeout included: a card that may have posted is still unavailable
        raise PaymentCardUndeliverable(f"payment card send failed: {type(exc).__name__}") from None
    if not getattr(result, "success", False):
        raise PaymentCardUndeliverable("payment card send failed")

    def retire(reason: str) -> None:
        turn_runner._schedule(adapter.retire_payment_card(request_id, reason), "payment card retire scheduling error")

    if not observe_gateway_settle(session_key, request_id, retire):
        # Settled while the card was being sent: retire it now.
        retire("resolved")
    if str(ctx._status_chat_id) != user_id:
        try:
            from gateway.run import _interim_metadata
            turn_runner._schedule(
                adapter.send(ctx._status_chat_id, GROUP_NOTICE, metadata=_interim_metadata(ctx._status_thread_metadata)),
                "payment notice scheduling error")
        except Exception:  # health: allow BLE001 -- the owner has the card; the notice is best effort
            logger.debug("payment approval group notice failed", exc_info=True)
