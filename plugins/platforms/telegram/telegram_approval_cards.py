"""dlz: approval-card identity and the owner-only payment card, kept out of the adapter facade.

Every approval card's buttons carry ``<prefix>:<choice>:<nonce>`` with a random single-use nonce
(``secrets.token_urlsafe(16)``), never a counter that restarts with the adapter. The nonce's record
binds the card to its request id, session, chat, thread, message, offered choices and, for a
payment card, the approver. A tap acts only when every binding matches; the record is popped on
the one tap that acts, so a replay, another card's nonce, or any button from before an adapter
restart (an empty map) resolves nothing.

``ea:`` is the exec-approval card; ``pa:`` the card-fill approval card (``send_payment_approval``),
sent only to the approver's private chat and answered only by the approver's own Telegram user.
Every tap is answered through ``callback_answer.answer_callback``: Telegram refusing the answer of an
expired query never escapes the handler, so a settled card still loses its buttons.
"""

from __future__ import annotations

import contextlib
import html
import logging
import secrets
import time
from typing import Any, Optional

from plugins.platforms.telegram.callback_answer import answer_callback

logger = logging.getLogger(__name__)

_MAX_CARDS = 512
_OWNER_ONLY = "Only the owner can answer this card-fill approval."
_INVALID = "This button does not match its approval card."
_ALREADY = "This approval was already answered or has expired."
_RETIRED = {
    "timeout": "Expired: nothing was approved.",
    "resolved": "Denied from another surface: nothing was approved.",
}
_WITHDRAWN = "Withdrawn: nothing was approved."


def _scalar(value: Any) -> Optional[str]:
    """A Telegram id as text; anything that is not an int or str (unset, a mock) is None."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return str(value)


class TelegramApprovalCardsMixin:
    """Nonce-bound approval cards for :class:`TelegramAdapter` (needs ``_send_prompt``,
    ``_callback_authorized``, ``_bot`` and ``MAX_MESSAGE_LENGTH``)."""

    def _approval_card_map(self) -> dict:
        cards = self.__dict__.get("_approval_cards")
        if cards is None:
            cards = self._approval_cards = {}
        return cards

    def _arm_approval_card(self, nonce: str, kind: str, msg: Any, *, chat_id: str, session_key: str,
                           request_id: Optional[str], choices, approver: Optional[str] = None,
                           text: str = "") -> None:
        """Record the card once it was sent: its identity is the sent message itself."""
        from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

        cards = self._approval_card_map()
        while len(cards) >= _MAX_CARDS:  # oldest first; an evicted card's tap resolves nothing
            cards.pop(next(iter(cards)))
        cards[nonce] = {
            "kind": kind, "session_key": session_key, "request_id": request_id, "choices": tuple(choices),
            "approver": approver, "text": text,
            "chat_id": _scalar(getattr(msg, "chat_id", None)) or _scalar(normalize_telegram_chat_id(chat_id)),
            "message_id": _scalar(getattr(msg, "message_id", None)),
            "thread_id": _scalar(getattr(msg, "message_thread_id", None)),
            "created": time.monotonic(),
        }

    async def _claim_approval_card(self, query, data: str, cb: dict, kind: str, unauthorized: str, *,
                                   already: str = _ALREADY, invalid: str = _INVALID):
        """``(record, choice, presser)`` for a tap that may act, after popping its record; else None
        (the tap was answered with a toast and nothing was resolved)."""
        parts = data.split(":", 2)
        if len(parts) != 3:
            await answer_callback(query, invalid)
            return None
        _, choice, nonce = parts
        if not await self._callback_authorized(query, cb, unauthorized):
            return None
        cards = self._approval_card_map()
        record = cards.get(nonce)
        if record is None or record["kind"] != kind:
            await answer_callback(query, already)
            return None
        message = getattr(query, "message", None)
        if (record["message_id"] is None
                or _scalar(getattr(message, "chat_id", None)) != record["chat_id"]
                or _scalar(getattr(message, "message_id", None)) != record["message_id"]
                or _scalar(getattr(message, "message_thread_id", None)) != record["thread_id"]
                or choice not in record["choices"]):
            await answer_callback(query, invalid)
            return None
        presser = f"telegram:{_scalar(getattr(query.from_user, 'id', None)) or ''}"
        if record["approver"] and presser != record["approver"]:
            # Not popped: the owner's own tap on this card still counts.
            await answer_callback(query, _OWNER_ONLY)
            return None
        cards.pop(nonce, None)
        return record, choice, presser

    async def send_payment_approval(self, chat_id: str, facts, request_id: str, session_key: str,
                                    approver: str):
        """The card-fill approval card in the approver's private chat: every mandatory fact or no
        card (``SendResult(success=False)``), buttons "Approve fill once" / "Deny"."""
        from gateway.platforms.base import SendResult
        from tools.payment_consent_prompt import APPROVE_LABEL, DENY_LABEL, PromptIncomplete, render_html
        from plugins.platforms.telegram.adapter import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode

        if not approver or approver != f"telegram:{chat_id}" or not request_id:
            return SendResult(success=False, error="the payment card goes only to the approver's private chat")

        def build():
            try:
                text = render_html(facts, budget_utf16=self.MAX_MESSAGE_LENGTH)
            except PromptIncomplete:
                return SendResult(success=False, error="payment prompt incomplete")
            nonce = secrets.token_urlsafe(16)
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(APPROVE_LABEL, callback_data=f"pa:once:{nonce}"),
                                              InlineKeyboardButton(DENY_LABEL, callback_data=f"pa:deny:{nonce}")]])

            def on_sent(msg):
                self._arm_approval_card(nonce, "pa", msg, chat_id=chat_id, session_key=session_key,
                                        request_id=request_id, choices=("once", "deny"), approver=approver,
                                        text=text)
            return text, keyboard, on_sent
        return await self._send_prompt("send_payment_approval", chat_id, None, build, parse_mode=ParseMode.HTML)

    async def retire_payment_card(self, request_id: str, reason: str) -> None:
        """The prompt settled without a tap on this card: drop its buttons and say so."""
        cards = self._approval_card_map()
        for nonce, record in list(cards.items()):
            if record["kind"] != "pa" or record["request_id"] != request_id:
                continue
            cards.pop(nonce, None)
            if self._bot is None or record["message_id"] is None:
                continue
            note = html.escape(_RETIRED.get(reason, _WITHDRAWN))
            kwargs = {"chat_id": int(record["chat_id"]) if record["chat_id"].lstrip("-").isdigit() else record["chat_id"],
                      "message_id": int(record["message_id"])}
            try:
                await self._bot.edit_message_text(text=f"{record['text']}\n\n— {note}", parse_mode="HTML",
                                                  reply_markup=None, **kwargs)
            except Exception:  # too long or gone: at least the buttons go
                with contextlib.suppress(Exception):
                    await self._bot.edit_message_reply_markup(reply_markup=None, **kwargs)

    async def _handle_payment_approval_callback(self, query, data: str, cb: dict) -> None:
        """``pa:<once|deny>:<nonce>`` — the owner answers a card-fill approval."""
        from plugins.platforms.telegram.adapter import _unauthorized
        from plugins.platforms.telegram.telegram_resolved_prompt import edit_keeping_prompt

        claimed = await self._claim_approval_card(query, data, cb, "pa", _unauthorized())
        if claimed is None:
            return
        record, choice, presser = claimed
        try:
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(record["session_key"], choice, request_id=record["request_id"],
                                             approver=presser)
        except Exception as exc:
            logger.error("Payment approval resolve failed: %s", type(exc).__name__)
            count = 0
        if not count:
            label = "Expired or already answered: nothing was approved."
        else:
            label = "Approved: fill once." if choice == "once" else "Denied: nothing will be filled."
        await answer_callback(query, label)
        await edit_keeping_prompt(self, query, label)
