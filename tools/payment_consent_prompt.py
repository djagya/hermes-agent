"""dlz: the card-fill approval prompt: facts, complete-or-nothing rendering, and the one entry point.

The card-fill path builds a :class:`PaymentPromptFacts` and calls :func:`request_payment_approval`.
The facts travel to the gateway as ``approval_data["payment"]``; the Telegram adapter renders them
with :func:`render_html` on a dedicated card where the owner talks to Sera (the session's group thread, or
the owner's private chat; ``gateway.payment_approval``). Rendering shows every
mandatory fact or raises :class:`PromptIncomplete`: it never truncates, and there is no plain-text
``/approve`` fallback. Only the owner's own Telegram button approves (``approvals.payment_approver``,
checked in ``tools.approval.resolve_gateway_approval``); every other surface can only deny, and another
group member's tap on the card changes nothing. When the card states the cart total, the owner's Approve is
also the go for one Pay press of that checkout (owner decision 2026-11-13): the card says so, and nothing else
authorizes a second press. A card without a stated total is fill-only: Pay needs the owner's go in chat.

No card value ever enters these facts: they hold the card's label, origins, field names and the
cart as Sera stated it.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Bounds applied before anything is rendered. A value outside them makes the prompt unavailable;
# nothing is cut to fit.
MAX_ITEMS = 8
MAX_ITEM_TEXT = 80
MAX_QUANTITY = 99
MAX_LABEL = 120
MAX_REF = 64
MAX_WHERE = 120
MAX_ORIGINS = 4
MAX_FIELDS = 8

RECEIVING_STATUSES = ("merchant", "reviewed", "UNREVIEWED")
PAGE_MATCH_STATES = ("matched", "not_found", "ambiguous", "not_checked")

# An origin as ``https_origin`` produces it: scheme, host (up to 253 characters), optional port.
_ORIGIN_RE = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?")
_FIELD_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_REF_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_AMOUNT_RE = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,3})?")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
# Control characters (C0, DEL, C1), bidi overrides/isolates, zero-width and line/paragraph separators.
_UNSAFE_TEXT_RE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff`]")

APPROVE_LABEL = "Approve: fill + Pay once"
# No total was stated: the card is fill-only, Pay needs the owner's go in chat.
APPROVE_LABEL_FILL_ONLY = "Approve: fill only"
DENY_LABEL = "Deny"
# The session's chat, when it is someone else's private chat (the card went to the owner's own chat).
GROUP_NOTICE = "Card-fill approval sent to the owner privately."
# The card's group topic is gone: the send fails closed (never the group's General topic) and the card goes to the owner's chat.
CARD_TOPIC_UNAVAILABLE = "payment card topic unavailable"
# Reply to a typed /approve that a waiting card fill stopped (FIFO) or that skipped it (/approve all).
TYPED_APPROVE_HELD = ("A card payment is waiting: only the owner's Approve button on its card approves it. "
                      "Typed /approve does not; /deny refuses it.")

PAGE_MATCH_LINES = {
    "matched": "Page check: the page shows this total (text check only).",
    "ambiguous": "Page check: AMBIGUOUS. This amount appears, but not clearly as the total.",
    "not_found": "Page check: NOT FOUND. Hermes did not find this total on the page. Check before approving.",
    "not_checked": "No cart was stated.",
}

# Fixed details for an unavailable prompt; each names the next step.
UNAVAILABLE_APPROVER = ("Card-fill approvals are off: approvals.payment_approver is not set to the owner's "
                        "Telegram user (telegram:<id>, also listed in TELEGRAM_ALLOWED_USERS). Ask the operator "
                        "to set it; do not try another way.")
UNAVAILABLE_SURFACE = ("Card-fill approvals are only shown as a Telegram card to the owner, and this session "
                       "cannot send one. Ask the user to run the checkout from Telegram.")
UNAVAILABLE_INCOMPLETE = ("The card-fill approval could not be shown completely, so it was not shown at all. "
                          "Tell the user; do not retry with different wording.")
UNAVAILABLE_UNDELIVERED = ("The card-fill approval card was not delivered to the owner on Telegram (the "
                           "send failed, Telegram did not confirm it in time, or the card could not show every "
                           "fact). If it posted late, it was withdrawn at once and a tap on it does nothing. Tell "
                           "the user the approval did not go through; do not retry the fill on your own.")
UNAVAILABLE_DEGRADED = ("The card-fill approval card was sent, but Hermes' Telegram connection dropped or reconnected "
                        "while it waited, so the owner's tap may not have arrived. Nothing was approved. Tell the user "
                        "what happened and ask once whether to try again; do not retry the fill on your own.")
UNAVAILABLE_FACTS = ("The card-fill approval facts were invalid (too many items, text too long, or a malformed "
                     "origin, amount or currency), so no prompt was shown. Correct the stated cart or tell the user.")


class PromptIncomplete(Exception):
    """The prompt cannot show every mandatory fact within the budget, or a fact is malformed."""


class FactsInvalid(ValueError):
    """A fact is outside its bounds; the prompt is unavailable rather than shortened."""


def _clean_text(value: Any, limit: int, what: str) -> str:
    """One line of display text: unsafe characters removed, whitespace collapsed, at most *limit*."""
    if not isinstance(value, str):
        raise FactsInvalid(f"{what} must be text")
    text = " ".join(_UNSAFE_TEXT_RE.sub(" ", value).split())
    if not text or len(text) > limit:
        raise FactsInvalid(f"{what} is empty or longer than {limit} characters")
    return text


@dataclass(frozen=True)
class ReceivingOrigin:
    """One origin that receives card fields, how it was reviewed, and which fields it gets."""
    origin: str
    status: str
    fields: tuple

    def __post_init__(self):
        if not isinstance(self.origin, str) or not self.origin:
            raise FactsInvalid("receiving origin must be text")
        if self.status not in RECEIVING_STATUSES:
            raise FactsInvalid("receiving status must be merchant, reviewed or UNREVIEWED")
        fields = tuple(self.fields)
        if not fields or len(fields) > MAX_FIELDS or any(not isinstance(f, str) or not _FIELD_RE.fullmatch(f)
                                                          for f in fields):
            raise FactsInvalid("receiving fields must be 1-8 field tokens")
        object.__setattr__(self, "fields", fields)


@dataclass(frozen=True)
class CartItem:
    description: str
    quantity: int

    def __post_init__(self):
        object.__setattr__(self, "description", _clean_text(self.description, MAX_ITEM_TEXT, "item description"))
        if type(self.quantity) is not int or not 1 <= self.quantity <= MAX_QUANTITY:
            raise FactsInvalid(f"item quantity must be 1-{MAX_QUANTITY}")


@dataclass(frozen=True)
class StatedCart:
    """The cart exactly as Sera stated it. Hermes does not verify it; the page check is separate."""
    items: tuple
    total: str
    currency: str

    def __post_init__(self):
        items = tuple(i if isinstance(i, CartItem) else CartItem(**i) for i in self.items)
        if not items or len(items) > MAX_ITEMS:
            raise FactsInvalid(f"the stated cart must have 1-{MAX_ITEMS} items")
        object.__setattr__(self, "items", items)
        if not isinstance(self.total, str) or not _AMOUNT_RE.fullmatch(self.total):
            raise FactsInvalid("the stated total must be a plain decimal amount")
        if not isinstance(self.currency, str) or not _CURRENCY_RE.fullmatch(self.currency):
            raise FactsInvalid("the stated currency must be a three-letter ISO code")


@dataclass(frozen=True)
class PageMatch:
    """Whether the page text shows the stated total: a sanity check, not verification."""
    state: str = "not_checked"
    where: str = ""

    def __post_init__(self):
        if self.state not in PAGE_MATCH_STATES:
            raise FactsInvalid("page match must be matched, not_found, ambiguous or not_checked")
        if self.where:
            object.__setattr__(self, "where", _clean_text(self.where, MAX_WHERE, "page-check location"))


@dataclass(frozen=True)
class PaymentPromptFacts:
    """Everything the owner must see before a card fill.

    ``ref`` is the full operation id; ``merchant_origin`` the top-level page; ``receiving`` every
    origin that gets card fields; ``cart`` Sera's stated cart (None on the reviewed path, which then
    needs ``page_match.state == "not_checked"``); ``expires_in_s`` the seconds left in the one-use
    window; ``unreviewed`` whether any receiving origin is UNREVIEWED."""
    ref: str
    card_label: str
    merchant_origin: str
    receiving: tuple
    expires_in_s: int
    cart: Optional[StatedCart] = None
    page_match: PageMatch = field(default_factory=PageMatch)
    unreviewed: bool = False

    def __post_init__(self):
        if not isinstance(self.ref, str) or not _REF_RE.fullmatch(self.ref):
            raise FactsInvalid("ref must be the operation id")
        object.__setattr__(self, "card_label", _clean_text(self.card_label, MAX_LABEL, "card label"))
        if not isinstance(self.merchant_origin, str) or not self.merchant_origin:
            raise FactsInvalid("merchant origin must be text")
        receiving = tuple(r if isinstance(r, ReceivingOrigin) else ReceivingOrigin(**r) for r in self.receiving)
        if not receiving or len(receiving) > MAX_ORIGINS or len({r.origin for r in receiving}) != len(receiving):
            raise FactsInvalid(f"there must be 1-{MAX_ORIGINS} distinct receiving origins")
        object.__setattr__(self, "receiving", receiving)
        if type(self.expires_in_s) is not int or not 1 <= self.expires_in_s <= 600:
            raise FactsInvalid("expires_in_s must be 1-600 seconds")
        cart = self.cart
        if cart is not None and not isinstance(cart, StatedCart):
            cart = StatedCart(**cart)
        object.__setattr__(self, "cart", cart)
        page_match = self.page_match if isinstance(self.page_match, PageMatch) else PageMatch(**self.page_match)
        object.__setattr__(self, "page_match", page_match)
        if (cart is None) != (page_match.state == "not_checked"):
            raise FactsInvalid("a stated cart needs a page check, and no cart means not_checked")
        if bool(self.unreviewed) != any(r.status == "UNREVIEWED" for r in receiving):
            raise FactsInvalid("unreviewed must match the receiving origins")

    def to_dict(self) -> dict:
        return {
            "ref": self.ref, "card_label": self.card_label, "merchant_origin": self.merchant_origin,
            "receiving": [{"origin": r.origin, "status": r.status, "fields": list(r.fields)} for r in self.receiving],
            "expires_in_s": self.expires_in_s,
            "cart": None if self.cart is None else {
                "items": [{"description": i.description, "quantity": i.quantity} for i in self.cart.items],
                "total": self.cart.total, "currency": self.cart.currency},
            "page_match": {"state": self.page_match.state, "where": self.page_match.where},
            "unreviewed": self.unreviewed,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PaymentPromptFacts:
        if not isinstance(data, dict):
            raise FactsInvalid("payment facts must be a mapping")
        expected = {"ref", "card_label", "merchant_origin", "receiving", "expires_in_s", "cart", "page_match",
                    "unreviewed"}
        if set(data) != expected:
            raise FactsInvalid("payment facts have missing or unknown keys")
        return cls(**{**data, "receiving": tuple(data["receiving"] or ())})


def _check_origins(facts: PaymentPromptFacts) -> None:
    for origin in (facts.merchant_origin, *(r.origin for r in facts.receiving)):
        if not _ORIGIN_RE.fullmatch(origin):
            raise PromptIncomplete("an origin is not a plain https origin")


def pays_once(facts: PaymentPromptFacts) -> bool:
    """Whether the owner's Approve also covers one Pay press: only when Sera stated the total and the page check
    matched it (peer r2 P2). Otherwise the card is fill-only and Pay needs the owner's go in chat; the payment lock
    binds the total only in this case (``browser_vault_payment._bind_approved_total``)."""
    return facts.cart is not None and facts.page_match.state == "matched"


def scope_line(facts: PaymentPromptFacts) -> str:
    """What one Approve tap does, in plain words: the fill and exactly one Pay press of this checkout, or (no
    stated total, or a total the page check did not match) the fill only."""
    if facts.cart is None:
        return (f"Approve: Sera fills the card only, at {facts.merchant_origin}. No total was stated, so this "
                "does not cover pressing Pay: Pay needs your go in chat.")
    if not pays_once(facts):
        return (f"Approve: Sera fills the card only, at {facts.merchant_origin}. Hermes did not find "
                f"{facts.cart.total} {facts.cart.currency} clearly as the page's total, so this does not cover "
                "pressing Pay: Pay needs your go in chat.")
    return (f"Approve: Sera fills the card and presses Pay once for {facts.cart.total} {facts.cart.currency} "
            f"at {facts.merchant_origin}.")


def _lines(facts: PaymentPromptFacts, esc, bold, code) -> list[str]:
    """The card body, one fact per line, in the order the owner reads it."""
    lines = [bold("Approve card payment?"), ""]
    if facts.unreviewed:
        lines += ["⚠️ " + bold("UNREVIEWED destination:") + " Hermes has not reviewed where some card "
                  "fields go. Check every origin below.", ""]
    lines += [bold(esc(scope_line(facts))), ""]
    lines += [f"Card: {bold(esc(facts.card_label))}",
              f"Merchant (top-level page): {code(facts.merchant_origin)}",
              "Card fields go to:"]
    for r in facts.receiving:
        lines.append(f"• {code(r.origin)} ({r.status}): {esc(', '.join(r.fields))}")
    lines.append("")
    if facts.cart is None:
        lines.append(PAGE_MATCH_LINES["not_checked"])
    else:
        lines.append("Cart (stated by Sera, not verified by Hermes):")
        lines += [f"• {i.quantity} × {esc(i.description)}" for i in facts.cart.items]
        lines.append(f"Total (stated by Sera): {bold(esc(facts.cart.total + ' ' + facts.cart.currency))}")
        where = f" Checked on {esc(facts.page_match.where)}." if facts.page_match.where else ""
        lines.append(esc(PAGE_MATCH_LINES[facts.page_match.state]) + where)
    covers = ("One tap covers one fill and one Pay press for this checkout; any other payment needs a new approval."
              if pays_once(facts) else
              "One tap covers one fill only: pressing Pay needs your go in chat. Any other payment needs a new approval.")
    lines += ["", f"{covers} Expires in {facts.expires_in_s} s.",
              "Hermes cannot verify what will be charged. HTTPS does not mean the seller is legitimate.",
              f"Ref: {code(facts.ref)}"]
    return lines


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def render_html(facts: PaymentPromptFacts, *, budget_utf16: int) -> str:
    """Telegram HTML card with every mandatory fact, or :class:`PromptIncomplete`. Never truncates."""
    _check_origins(facts)
    text = "\n".join(_lines(facts, html.escape, lambda s: f"<b>{s}</b>", lambda s: f"<code>{html.escape(s)}</code>"))
    text = "💳 " + text
    if _utf16_len(text) > budget_utf16:
        raise PromptIncomplete("the payment card does not fit one message")
    return text


def render_plain(facts: PaymentPromptFacts) -> str:
    """The same facts as plain text, for logs, hooks and the approval record (never shown as a prompt)."""
    targets = ", ".join(r.origin for r in facts.receiving)
    plain = _lines(facts, lambda s: s, lambda s: s, lambda s: s)
    return "\n".join([f"Card fields will be sent to {targets}.", *plain])


def summary(facts: PaymentPromptFacts) -> str:
    return f"Fill '{facts.card_label}' on {facts.merchant_origin}"


@dataclass(frozen=True)
class PaymentConsent:
    """The owner's answer. ``outcome`` is ``accept`` | ``deny`` | ``timeout`` | ``unavailable``;
    ``reason`` the elicitation reason (``accepted`` | ``refused`` | ``timeout`` | ``unavailable``)
    that ``browser_vault_payment._consent_not_given`` reads; ``detail`` a fixed next-step text."""
    outcome: str
    reason: str
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.outcome == "accept"

    @property
    def verdict(self) -> str:
        """The legacy elicitation verdict for this outcome."""
        return {"accept": "accept", "timeout": "cancel"}.get(self.outcome, "decline")


_OUTCOME_BY_REASON = {"accepted": "accept", "refused": "deny", "timeout": "timeout", "unavailable": "unavailable"}


def request_payment_approval(facts: PaymentPromptFacts, *, timeout_seconds: Optional[int] = None) -> PaymentConsent:
    """Ask the owner to approve one card fill and block until they answer, the window closes, or the
    prompt cannot be shown. Never raises; never approves on its own."""
    from tools import approval_prompt

    try:
        payload = facts.to_dict()
        message, description = summary(facts), render_plain(facts)
    except Exception:  # health: allow BLE001 -- a malformed fact makes the prompt unavailable, never partial
        return PaymentConsent("unavailable", "unavailable", UNAVAILABLE_FACTS)
    detail: dict = {}
    try:
        # Looked up on the module at call time, so the existing consent seam stays the one patch point.
        verdict = approval_prompt.request_elicitation_consent(
            message, description, surface="vault-payment", title="Approve card fill on this merchant?",
            outcome=detail, timeout_seconds=timeout_seconds, payment=payload)
    except Exception:  # health: allow BLE001 -- a failed prompt is unavailable consent, not an approval
        return PaymentConsent("unavailable", "unavailable", UNAVAILABLE_INCOMPLETE)
    reason = detail.get("reason")
    if reason not in _OUTCOME_BY_REASON:
        reason = {"accept": "accepted", "cancel": "timeout"}.get(verdict, "refused")
    outcome = _OUTCOME_BY_REASON[reason]
    if (outcome == "accept") != (verdict == "accept"):  # a reason that disagrees with the verdict never accepts
        outcome, reason = "deny", "refused"
    return PaymentConsent(outcome, reason, str(detail.get("detail") or ""))


def facts_for_inspection(inspected, *, card_label: str, cart: Optional[StatedCart] = None) -> PaymentPromptFacts:
    """Facts for a fill: the inspected documents' origins and fields. dlz (Track B): with ``cart`` (Sera's stated
    cart), the page check the inspection pinned (``inspected.page_match``: state and where it looked) goes with it;
    without one the card says no cart was stated."""
    from agent.vault_payment_policy import provider_for_origin

    merchant = inspected.merchant_origin
    by_origin: dict[str, list] = {}
    for document in inspected.documents:
        by_origin.setdefault(document.origin, []).extend(c.token for c in document.controls)
    receiving = tuple(
        ReceivingOrigin(origin, "merchant" if origin == merchant
                        else "reviewed" if provider_for_origin(origin) is not None else "UNREVIEWED",
                        tuple(dict.fromkeys(tokens)))
        for origin, tokens in by_origin.items())
    return PaymentPromptFacts(
        ref=inspected.operation_id, card_label=card_label, merchant_origin=merchant, receiving=receiving,
        expires_in_s=max(1, min(600, int(inspected.expires_at - time.monotonic()))),
        cart=cart,
        page_match=PageMatch(*getattr(inspected, "page_match", ("not_checked", ""))) if cart is not None else PageMatch(),
        unreviewed=any(r.status == "UNREVIEWED" for r in receiving))
