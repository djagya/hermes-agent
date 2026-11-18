"""Trusted card parsing. Fixed failures keep manager output out of logs/results."""

from __future__ import annotations

import re
from datetime import date


class CardInvalid(ValueError):
    """dlz (11-18): a saved card the fill cannot use, with a fixed code (``card_fields_invalid``, ``card_expired``,
    ``card_number_invalid``) the refusal may name. Carries no value."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def validate_card(secret: dict[str, str]) -> dict[str, str]:
    number = re.sub(r"[ -]", "", str(secret.get("card_number", "")))
    cvc = str(secret.get("cvc", ""))
    month = str(secret.get("exp_month", ""))
    year = str(secret.get("exp_year", ""))
    if not (
        re.fullmatch(r"[0-9]{12,19}", number)
        and re.fullmatch(r"[0-9]{3,4}", cvc)
        and re.fullmatch(r"[0-9]{1,2}", month)
        and re.fullmatch(r"[0-9]{4}", year)
    ):
        raise CardInvalid("Invalid card fields", "card_fields_invalid")
    today = date.today()
    if not 1 <= int(month) <= 12 or (int(year), int(month)) < (today.year, today.month):
        raise CardInvalid("Invalid card expiry", "card_expired")
    digits = [int(n) for n in number[::-1]]
    if (
        sum(
            n if i % 2 == 0 else (n * 2 if n < 5 else n * 2 - 9)
            for i, n in enumerate(digits)
        )
        % 10
    ):
        raise CardInvalid("Invalid card number", "card_number_invalid")
    out = {
        "card_number": number,
        "cvc": cvc,
        "exp_month": month.zfill(2),
        "exp_year": year,
    }
    if secret.get("cardholder_name"):
        out["cardholder_name"] = str(secret["cardholder_name"])
    # dlz (D-0359 a, 2026-11-18): the billing postcode is filled like the holder when the item has one; not a
    # secret on its own (never in the payment window). A value that is not postcode-shaped is left out (the fill
    # then reports the field as skipped), never a reason to refuse the card.
    postcode = billing_postcode(secret.get("billing_postal_code"))
    if postcode:
        out["billing_postal_code"] = postcode
    return out


_POSTCODE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 -]{0,10}[A-Za-z0-9])?")


def billing_postcode(value) -> str:
    """A billing postcode as the card form takes it (2-12 letters, digits, inner spaces or dashes), or ``""``."""
    text = " ".join(str(value or "").split())
    return text if len(text) >= 2 and _POSTCODE.fullmatch(text) else ""


# 1Password's Credit Card template has no postcode field: a custom field on the item carries it. Labels it may have
# (lower case, spaces and punctuation folded to one space).
_OP_POSTCODE_LABELS = frozenset({
    "billing postal code", "billing postcode", "billing post code", "billing zip", "billing zip code",
    "postal code", "postcode", "post code", "zip", "zip code", "plz", "postleitzahl",
})


def _op_postcode(fields: list) -> str:
    """The item's one billing postcode field (by id ``billing_postal_code`` or a label above), or ``""`` when it
    has none or two different ones (the fill then skips the field and says so)."""
    found = set()
    for field in fields:
        label = " ".join(re.sub(r"[^a-z0-9]+", " ", str(field.get("label") or "").lower()).split())
        if field.get("id") == "billing_postal_code" or label in _OP_POSTCODE_LABELS:
            if isinstance(field.get("value"), str) and field["value"].strip():
                found.add(billing_postcode(field["value"]))
    return found.pop() if len(found) == 1 else ""


def parse_onepassword_card(
    item: dict, *, item_id: str, vault_id: str
) -> dict[str, str]:
    if (
        item.get("category") != "CREDIT_CARD"
        or item.get("id") != item_id
        or (item.get("vault") or {}).get("id") != vault_id
    ):
        raise ValueError("Card identity changed")
    fields = {}
    for field in item.get("fields") or []:
        key = field.get("id")
        if key in {"ccnum", "cvv", "expiry", "cardholder"}:
            if key in fields or not isinstance(field.get("value"), str):
                raise ValueError("Ambiguous card fields")
            fields[key] = field["value"]
    expiry = fields.get("expiry", "")
    # op's Credit Card template uses MONTH_YEAR: YYYYMM.
    if not re.fullmatch(r"[0-9]{6}", expiry):
        raise ValueError("Invalid card expiry")
    return validate_card({
        "card_number": fields.get("ccnum", ""),
        "cvc": fields.get("cvv", ""),
        "exp_year": expiry[:4],
        "exp_month": expiry[4:],
        "cardholder_name": fields.get("cardholder", ""),
        "billing_postal_code": _op_postcode(item.get("fields") or []),
    })
