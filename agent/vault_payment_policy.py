"""Native card metadata and canonical origin checks; no metadata grants fill."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

@dataclass(frozen=True)
class PaymentProvider:
    """One reviewed hosted card-field product. ``origins`` are the exact PRODUCTION origins that host
    its card inputs (never its script, API, wallet or 3DS origins); a checkout uses one of them.
    ``max_documents`` bounds the card documents of one checkout (merchant page included), set from the
    provider's observed topology. Sandbox origins are never listed: a sandbox frame must not receive
    a real card. Evidence per row: docs/payment-providers.md."""

    name: str
    origins: tuple
    max_documents: int = 4
    # Path prefixes of the card-field documents when the origin also serves other pages (PayPal's
    # www.paypal.com hosts card fields AND its wallet and login); empty = any path on the origin.
    paths: tuple = ()

    def admits(self, url: str) -> bool:
        if not self.paths:
            return True
        path = urlsplit(url).path
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.paths)


# Always on (the reviewed set before provider profiles existed). Datatrans Secure Fields puts the
# number and the CVC in separate pay.datatrans.com frames; Shopify checkout puts every card field in
# its own checkout.pci.shopifyinc.com frame.
DEFAULT_PROVIDERS = (
    PaymentProvider("stripe-elements", ("https://js.stripe.com",)),
    PaymentProvider("datatrans-secure-fields", ("https://pay.datatrans.com",)),
    PaymentProvider("shopify-checkout", ("https://checkout.pci.shopifyinc.com",)),
)
# Reviewed, OFF until the operator lists the name in config ``vault.payment_providers``. Each row was
# rendered from the provider's real sandbox and filled by the native path; production origins and
# paths come from the provider's docs/SDK (website/docs/developer-guide/payment-providers.md).
OPT_IN_PROVIDERS = (
    PaymentProvider("worldpay-hpp", ("https://payments.worldpay.com",), 2, ("/app/hpp/",)),
    PaymentProvider("worldpay-access-checkout", ("https://access.worldpay.com",), 4, ("/access-checkout/",)),
    PaymentProvider("braintree-hosted-fields", ("https://assets.braintreegateway.com",), 4, ("/web/",)),
    PaymentProvider("paypal-card-fields", ("https://www.paypal.com",), 4, ("/smart/card-field",)),
    PaymentProvider("checkoutcom-frames", ("https://js.checkout.com",), 4, ("/framesv2/",)),
    PaymentProvider("mollie-components", ("https://js.mollie.com",), 4, ("/v1/component/",)),
    PaymentProvider("unzer-ui-v2", ("https://static-cc.unzer.com",), 2, ("/v2/card-ui/",)),
    PaymentProvider("unzer-ui-v1", ("https://payment.unzer.com",), 4, ("/v1/cardframe/",)),
    PaymentProvider("saferpay-fields", ("https://www.saferpay.com",), 4, ("/Fields/",)),
)
PAYMENT_PROVIDERS = DEFAULT_PROVIDERS + OPT_IN_PROVIDERS
PAYMENT_PSP_ORIGINS = tuple(o for p in DEFAULT_PROVIDERS for o in p.origins)


def enabled_providers() -> tuple:
    """Default providers plus the opt-in ones named in ``vault.payment_providers``; unknown names
    are ignored (a typo never admits anything)."""
    try:
        from hermes_cli.config import cfg_get, load_config
        names = cfg_get(load_config(), "vault", "payment_providers", default=[]) or []
    except Exception:  # health: allow BLE001 -- unreadable config means the default set, never more
        names = []
    wanted = {str(n) for n in names} if isinstance(names, (list, tuple)) else set()
    return DEFAULT_PROVIDERS + tuple(p for p in OPT_IN_PROVIDERS if p.name in wanted)


def reviewed_origins() -> tuple:
    extra = tuple(o for p in enabled_providers() if p not in DEFAULT_PROVIDERS for o in p.origins)
    return tuple(dict.fromkeys(PAYMENT_PSP_ORIGINS + extra))


def provider_for_origin(origin: str) -> PaymentProvider | None:
    return next((p for p in PAYMENT_PROVIDERS if origin in p.origins), None)


def effective_frame_origins(override: tuple = ()) -> tuple:
    """The hosted origins a card may be filled into. Item metadata can only NARROW the reviewed
    set: an override naming an unreviewed origin is refused, never silently admitted."""
    reviewed = reviewed_origins()
    if not override:
        return reviewed
    if any(o not in reviewed for o in override):
        raise PaymentPolicyError("Card metadata names an unreviewed payment origin")
    return tuple(dict.fromkeys(override))


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
