"""Trusted card parsing. Fixed failures keep manager output out of logs/results."""

from __future__ import annotations

import re
from datetime import date


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
        raise ValueError("Invalid card fields")
    today = date.today()
    if not 1 <= int(month) <= 12 or (int(year), int(month)) < (today.year, today.month):
        raise ValueError("Invalid card expiry")
    digits = [int(n) for n in number[::-1]]
    if (
        sum(
            n if i % 2 == 0 else (n * 2 if n < 5 else n * 2 - 9)
            for i, n in enumerate(digits)
        )
        % 10
    ):
        raise ValueError("Invalid card number")
    out = {
        "card_number": number,
        "cvc": cvc,
        "exp_month": month.zfill(2),
        "exp_year": year,
    }
    if secret.get("cardholder_name"):
        out["cardholder_name"] = str(secret["cardholder_name"])
    return out


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
    })
