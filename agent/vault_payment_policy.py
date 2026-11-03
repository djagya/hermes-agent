"""Native card metadata and canonical origin checks; no metadata grants fill."""

from __future__ import annotations

import re
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
    # Paths of the provider's own non-card documents that may CONTAIN a card-field document (PayPal's
    # guest card form renders inside its /smart/buttons frame). Never fill targets themselves.
    ancestor_paths: tuple = ()

    @staticmethod
    def _on(url: str, prefixes: tuple) -> bool:
        path = urlsplit(url).path
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)

    def admits(self, url: str) -> bool:
        return not self.paths or self._on(url, self.paths)

    def admits_ancestor(self, url: str) -> bool:
        """A frame on this origin that may sit above a card-field document: a card path, a listed
        ancestor path, or an about:blank/srcdoc frame (it carries the origin of the document that made it)."""
        return self.admits(url) or self._on(url, self.ancestor_paths) or url in ("about:blank", "about:srcdoc")


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
    # SDK card-fields (/smart/card-field) and the Buttons guest card form (/smart/card-fields, rendered
    # inside /smart/buttons; captured 2026-11-03 on a live merchant checkout).
    PaymentProvider("paypal-card-fields", ("https://www.paypal.com",), 4, ("/smart/card-field", "/smart/card-fields"),
                    ("/smart/buttons",)),
    PaymentProvider("checkoutcom-frames", ("https://js.checkout.com",), 4, ("/framesv2/",)),
    PaymentProvider("mollie-components", ("https://js.mollie.com",), 4, ("/v1/component/",)),
    PaymentProvider("unzer-ui-v2", ("https://static-cc.unzer.com",), 2, ("/v2/card-ui/",)),
    PaymentProvider("unzer-ui-v1", ("https://payment.unzer.com",), 4, ("/v1/cardframe/",)),
    PaymentProvider("saferpay-fields", ("https://www.saferpay.com",), 4, ("/Fields/",)),
    # PAYONE Client API hosted iFrames: one frame per field (number, CVC, expiry month and year as
    # <select>), the holder on the merchant page: 5 documents. Captured 2026-11-03 on a live checkout.
    PaymentProvider("payone-hosted-iframes", ("https://secure.pay1.de",), 5,
                    ("/client-api/js/v1/payone_iframe.html",)),
)
PAYMENT_PROVIDERS = DEFAULT_PROVIDERS + OPT_IN_PROVIDERS
# Hosted card-field products reviewed and found unsupported (payment-providers.md). Never admitted;
# named in a refusal so the cause reads as the payment provider, not the shop.
UNSUPPORTED_PROVIDERS = {
    "https://payment.direct.worldline-solutions.com": "Worldline Direct Hosted Tokenization Page, not supported",
    "https://www.computop-paygate.com": "Computop Paygate, not supported",
}
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


def coverage_hint(tree: tuple, merchant: str, allowed: tuple) -> str:
    """Why a checkout yielded no complete reviewed card document, from frame origins and paths only
    (nothing is read from an unreviewed frame). ``tree`` rows are ``(id, loader, url, origin,
    parent)`` with the browser's security origin. Only constant provider text and a sanitized path
    of a reviewed origin appear; '' when nothing specific applies."""
    notes = []
    for _fid, _loader, url, origin, parent in tree:
        if not parent or origin == merchant:
            continue
        provider = provider_for_origin(origin)
        if origin in UNSUPPORTED_PROVIDERS:
            notes.append(f"card frames on {origin}: {UNSUPPORTED_PROVIDERS[origin]}")
        elif provider is not None and origin not in allowed:
            notes.append(f"{origin} is reviewed provider {provider.name}, not enabled in vault.payment_providers")
        elif provider is not None and not provider.admits(url):
            path = re.sub(r"[^A-Za-z0-9/_.-]", "", urlsplit(url).path)[:40]
            notes.append(f"{origin}{path} is not a {provider.name} card-field document "
                         "(wallet, buttons and guest-checkout pages are out of scope)")
    return "; ".join(dict.fromkeys(notes))[:400]


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
