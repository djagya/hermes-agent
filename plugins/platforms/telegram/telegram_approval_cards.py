"""dlz: approval-card identity and the owner-only payment card, kept out of the adapter facade.

Every approval card's buttons carry ``<prefix>:<choice>:<nonce>`` with a random single-use nonce
(``secrets.token_urlsafe(16)``), never a counter that restarts with the adapter. The nonce's record
binds the card to its request id, session, chat, thread, message, offered choices and, for a
payment card, the approver. A tap acts only when every binding matches; the record is popped on
the one tap that acts, so a replay, another card's nonce, or any button from before an adapter
restart (an empty map) resolves nothing.

``ea:`` is the exec-approval card; ``pa:`` the card-fill approval card (``send_payment_approval``), sent to the
approver's private chat or to a group chat (the session's own chat and thread, ``gateway.payment_approval``) and
answered only by the approver's own Telegram user: anyone else's tap, Deny included, changes nothing.
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
_OWNER_ONLY = "Only the owner can approve this card payment."
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
        def refused(why: str) -> None:
            # dlz: a payment tap that resolves nothing leaves a trace (no nonce, no card text). "no armed card"
            # after a reconnect means this adapter is not the one that sent the card (it was rebuilt).
            if kind == "pa":
                logger.info("[%s] Payment approval tap refused (%s, message_id=%s)", self.name,
                            why, _scalar(getattr(getattr(query, "message", None), "message_id", None)))

        parts = data.split(":", 2)
        if len(parts) != 3:
            refused("malformed")
            await answer_callback(query, invalid)
            return None
        _, choice, nonce = parts
        if not await self._callback_authorized(query, cb, unauthorized):
            refused("unauthorized")
            return None
        cards = self._approval_card_map()
        record = cards.get(nonce)
        if record is None or record["kind"] != kind:
            refused("no armed card on this adapter")
            await answer_callback(query, already)
            return None
        message = getattr(query, "message", None)
        if (record["message_id"] is None
                or _scalar(getattr(message, "chat_id", None)) != record["chat_id"]
                or _scalar(getattr(message, "message_id", None)) != record["message_id"]
                or _scalar(getattr(message, "message_thread_id", None)) != record["thread_id"]
                or choice not in record["choices"]):
            refused("binding mismatch")
            await answer_callback(query, invalid)
            return None
        presser = f"telegram:{_scalar(getattr(query.from_user, 'id', None)) or ''}"
        if record["approver"] and presser != record["approver"]:
            refused("not the owner")
            # Not popped: the owner's own tap on this card still counts.
            await answer_callback(query, _OWNER_ONLY)
            return None
        cards.pop(nonce, None)
        return record, choice, presser

    async def send_payment_approval(self, chat_id: str, facts, request_id: str, session_key: str,
                                    approver: str, metadata: Optional[dict] = None):
        """The card-fill approval card in the approver's private chat or a group chat (in the thread
        *metadata* names): every mandatory fact or no card (``SendResult(success=False)``), buttons
        ``APPROVE_LABEL`` / ``DENY_LABEL``. Any other private chat (someone else's) never gets it."""
        from gateway.platforms.base import SendResult
        from tools.payment_consent_prompt import APPROVE_LABEL, DENY_LABEL, PromptIncomplete, render_html
        from plugins.platforms.telegram.adapter import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
        from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

        platform, _, user_id = str(approver or "").partition(":")
        target = normalize_telegram_chat_id(chat_id)
        own_chat = isinstance(target, int) and str(target) == user_id
        group = isinstance(target, int) and target < 0
        if (platform != "telegram" or not user_id.isascii() or not user_id.isdigit() or not (own_chat or group)
                or not request_id):
            return SendResult(success=False,
                              error="the payment card goes only to the approver's private chat or a group chat")

        def build():
            try:
                text = render_html(facts, budget_utf16=self.MAX_MESSAGE_LENGTH)
            except PromptIncomplete:
                return SendResult(success=False, error="payment prompt incomplete")
            nonce = secrets.token_urlsafe(16)
            # One button per row: the approve label is never cut on a phone.
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(APPROVE_LABEL, callback_data=f"pa:once:{nonce}")],
                                             [InlineKeyboardButton(DENY_LABEL, callback_data=f"pa:deny:{nonce}")]])

            def on_sent(msg):
                self._arm_approval_card(nonce, "pa", msg, chat_id=chat_id, session_key=session_key,
                                        request_id=request_id, choices=("once", "deny"), approver=approver,
                                        text=text)
            return text, keyboard, on_sent
        return await self._send_prompt("send_payment_approval", chat_id, metadata, build, parse_mode=ParseMode.HTML,
                                       thread_id=self._metadata_thread_id(metadata), reply_to_mode=self._reply_to_mode)

    async def remind_payment_card(self, request_id: str, text: str) -> bool:
        """One plain *text* replying to the armed payment card of *request_id*, in its chat and thread. False (nothing
        sent) once the card is gone: tapped, retired or evicted."""
        record = next((r for r in self._approval_card_map().values()
                       if r["kind"] == "pa" and r["request_id"] == request_id), None)
        if record is None or record["message_id"] is None or self._bot is None:
            return False
        from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

        kwargs = {"chat_id": normalize_telegram_chat_id(record["chat_id"]), "text": text,
                  "reply_to_message_id": int(record["message_id"])}
        if record["thread_id"] is not None:
            kwargs["message_thread_id"] = int(record["thread_id"])
        try:
            await self._send_message_with_thread_fallback(**kwargs)
        except Exception as exc:  # health: allow BLE001 -- best effort: the card itself is still live
            logger.warning("[%s] Payment approval reminder failed: %s", self.name, type(exc).__name__)
            return False
        return True

    def payment_delivery_mark(self) -> int:
        """The polling generation now; :meth:`payment_delivery_disrupted` compares against it."""
        return getattr(self, "_polling_generation", 0)

    def payment_delivery_disrupted(self, mark: int) -> bool:
        """Since *mark*: polling restarted (a new generation: reconnect or disconnect), is not yet proven, or this
        adapter was retired or replaced. Any of these may have kept the owner's tap from reaching the card's
        record, which lives only on this adapter. Reads attributes only; safe from the agent thread."""
        return (getattr(self, "_polling_generation", 0) != mark or self.send_path_degraded
                or getattr(self, "_bot", None) is None or self.has_fatal_error
                or self._replacement_telegram_adapter() is not None)

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
        from plugins.platforms.telegram.telegram_resolved_prompt import edit_keeping_prompt

        # Every tap but the owner's is answered with the owner-only text, the callback allowlist's refusal too.
        claimed = await self._claim_approval_card(query, data, cb, "pa", _OWNER_ONLY)
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
        logger.info("[%s] Payment approval callback (request_id=%s, message_id=%s, choice=%s, applied=%s)",
                    self.name, record["request_id"], record["message_id"], choice, bool(count))
        if not count:
            label = "Expired or already answered: nothing was approved."
        else:
            label = ("Approved: Sera fills the card and presses Pay once." if choice == "once"
                     else "Denied: nothing will be filled or paid.")
        await answer_callback(query, label)
        await edit_keeping_prompt(self, query, label)
