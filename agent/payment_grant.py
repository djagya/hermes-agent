"""One in-memory authorization for a single card checkout.

A general-purpose card is not owned by a merchant. The grant records the
exact charge the user just confirmed (session, browser task, card handle,
merchant origin, currency, amount, items, recipient, desktop-lease epoch)
and allows one fill plus one submit until it expires. It is not written to
disk and it never contains card data.

An identical still-open grant is reused so a retry does not ask again.
A used submit, a different envelope, an expired grant, or a lease epoch
change does not carry over.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

PAYMENT_GRANT_TTL_SECONDS = 600

_AMOUNT = re.compile(r"^(?:0|[1-9]\d*)\.\d{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")

_LOCK = threading.Lock()
_GRANTS: dict[tuple, "Grant"] = {}


class EnvelopeError(ValueError):
    """The checkout description itself is unusable. Safe to show the agent."""


@dataclass(frozen=True)
class PaymentEnvelope:
    session_key: str
    task_id: str
    handle: str
    merchant_origin: str
    currency: str
    amount: str
    recipient: str
    items: Tuple[Tuple[str, int], ...]
    lease_epoch: int
    card_label: str

    def key(self) -> tuple:
        return (
            self.session_key,
            self.task_id,
            self.handle,
            self.merchant_origin,
            self.currency,
            self.amount,
            self.recipient,
            self.items,
            self.lease_epoch,
        )


@dataclass
class Grant:
    envelope: PaymentEnvelope
    expires_at: float
    submit_used: bool = False


@dataclass(frozen=True)
class AuthResult:
    status: str  # accept | decline | cancel | replay | stale
    reused: bool = False


def _one_line(value: object, *, limit: int, what: str) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > limit:
        raise EnvelopeError(f"{what} is missing or too long")
    return text


def build_envelope(
    *,
    session_key: str,
    task_id: str,
    handle: str,
    card_label: str,
    merchant_origin: str,
    currency: object,
    amount: object,
    recipient: object,
    items: object,
    lease_epoch: int,
) -> PaymentEnvelope:
    if not isinstance(amount, str) or not _AMOUNT.match(amount) or amount == "0.00":
        raise EnvelopeError(
            "amount must be a non-zero string with two decimals, e.g. 94.26"
        )
    if not isinstance(currency, str) or not _CURRENCY.match(currency.strip().upper()):
        raise EnvelopeError("currency must be a 3-letter code, e.g. EUR")
    if (
        not merchant_origin.startswith("https://")
        or "/" in merchant_origin.split("://", 1)[-1]
    ):
        raise EnvelopeError("checkout origin must be https")
    if (
        not isinstance(items, Sequence)
        or isinstance(items, (str, bytes))
        or not items
        or len(items) > 12
    ):
        raise EnvelopeError("items must be 1 to 12 {label, count} entries")
    parsed: list[tuple[str, int]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise EnvelopeError("each item needs a label and a count")
        count = raw.get("count")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= 99
        ):
            raise EnvelopeError("item count must be an integer from 1 to 99")
        parsed.append((
            _one_line(raw.get("label"), limit=120, what="item label"),
            count,
        ))
    return PaymentEnvelope(
        session_key=session_key,
        task_id=task_id,
        handle=handle,
        merchant_origin=merchant_origin,
        currency=currency.strip().upper(),
        amount=amount,
        recipient=_one_line(recipient, limit=200, what="recipient"),
        items=tuple(parsed),
        lease_epoch=lease_epoch,
        card_label=_one_line(card_label, limit=120, what="card label"),
    )


def _live(grant: Optional[Grant], now: float) -> Optional[Grant]:
    if grant is None or grant.expires_at <= now:
        return None
    return grant


def confirmation_text(envelope: PaymentEnvelope) -> tuple[str, str]:
    """(message, description) for the existing approval surface. No card data."""
    host = envelope.merchant_origin.split("://", 1)[-1]
    lines = [f"- {count} × {label}" for label, count in envelope.items]
    message = f"Pay {envelope.amount} {envelope.currency} on {host} with {envelope.card_label}"
    description = (
        "This confirms one card fill and one submit for this exact charge.\n"
        f"To: {envelope.recipient}\n"
        + "\n".join(lines)
        + "\nA different merchant, amount, card, item, recipient, or a later takeover needs a new confirmation. "
        "The card number and CVC never enter the chat."
    )
    return message, description


def authorize(
    envelope: PaymentEnvelope,
    *,
    consent: Callable[[str, str], str],
    now: Optional[float] = None,
) -> AuthResult:
    """Return the grant decision. ``consent`` is called only when no open grant matches.

    ``consent`` must return ``accept``, ``decline``, or ``cancel`` and is the
    real approval surface (Telegram button / CLI prompt), not a synthetic callback.
    """
    current = time.time() if now is None else now
    key = envelope.key()
    with _LOCK:
        grant = _live(_GRANTS.get(key), current)
        if grant is None:
            _GRANTS.pop(key, None)
        elif grant.submit_used:
            logger.info(
                "payment grant replay session=%s origin=%s amount=%s %s",
                envelope.session_key,
                envelope.merchant_origin,
                envelope.amount,
                envelope.currency,
            )
            return AuthResult("replay")
        else:
            return AuthResult("accept", reused=True)

    message, description = confirmation_text(envelope)
    choice = consent(message, description)
    decided = time.time() if now is None else now
    if choice != "accept":
        status = "cancel" if choice == "cancel" else "decline"
        logger.info(
            "payment grant %s session=%s origin=%s amount=%s %s",
            status,
            envelope.session_key,
            envelope.merchant_origin,
            envelope.amount,
            envelope.currency,
        )
        return AuthResult(status)

    with _LOCK:
        grant = _live(_GRANTS.get(key), decided)
        if grant is not None and grant.submit_used:
            return AuthResult("replay")
        if grant is None:
            _GRANTS[key] = Grant(
                envelope=envelope, expires_at=decided + PAYMENT_GRANT_TTL_SECONDS
            )
            logger.info(
                "payment grant accept session=%s origin=%s amount=%s %s epoch=%s",
                envelope.session_key,
                envelope.merchant_origin,
                envelope.amount,
                envelope.currency,
                envelope.lease_epoch,
            )
            return AuthResult("accept", reused=False)
        return AuthResult("accept", reused=True)


def consume_submit(envelope: PaymentEnvelope, *, now: Optional[float] = None) -> bool:
    """Mark the one allowed submit used. False when there is nothing left to use."""
    current = time.time() if now is None else now
    with _LOCK:
        grant = _live(_GRANTS.get(envelope.key()), current)
        if grant is None or grant.submit_used:
            return False
        grant.submit_used = True
        return True


def release_submit(envelope: PaymentEnvelope) -> None:
    """Undo ``consume_submit`` when the click provably did not happen."""
    with _LOCK:
        grant = _GRANTS.get(envelope.key())
        if grant is not None:
            grant.submit_used = False


def reset_for_tests() -> None:
    with _LOCK:
        _GRANTS.clear()
