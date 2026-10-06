"""Native card metadata and canonical origin checks; no metadata grants fill."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

PAYMENT_PSP_ORIGINS = ("https://js.stripe.com",)


class PaymentPolicyError(ValueError):
    """Fixed-message refusal; never include supplied metadata or values."""


def https_origin(value: str) -> str:
    """Require a canonical live HTTPS origin, not a URL or inferred origin."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise PaymentPolicyError("HTTPS origin required")
    try:
        parts = urlsplit(value)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise PaymentPolicyError("HTTPS origin required") from None
    if (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or not host.isascii()
        or any(c in host for c in "\\%")
    ):
        raise PaymentPolicyError("HTTPS origin required")
    authority = f"[{host}]" if ":" in host else host
    canonical = "https://" + authority + (f":{port}" if port not in (None, 443) else "")
    if canonical != value or port == 0:
        raise PaymentPolicyError("Canonical HTTPS origin required")
    return value


@dataclass(frozen=True)
class PaymentMetadata:
    handle: str


def onepassword_payment_metadata(
    item: Mapping, configured_vault: str = ""
) -> PaymentMetadata | None:
    """Read category/opaque identity only. Bank URLs and tags grant no scope."""
    if item.get("category") != "CREDIT_CARD":
        return None
    item_id = item.get("id")
    vault = item.get("vault")
    vault_id = vault.get("id") if isinstance(vault, Mapping) else None
    vault_id = vault_id or configured_vault
    if not isinstance(item_id, str) or not item_id or ":" in item_id:
        return None
    if not isinstance(vault_id, str) or not vault_id or ":" in vault_id:
        return None
    return PaymentMetadata(f"op:{vault_id}:{item_id}")
