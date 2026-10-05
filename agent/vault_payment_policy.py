"""Metadata and legacy whole-envelope contracts.

A snapshot is not a whole-checkout atomic commit. That global primitive remains
unavailable. Native human-confirmed, single-document filling is implemented in
tools.browser_supervisor_payment and tools.browser_vault_payment instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

GLOBAL_PAYMENT_TAG = "hermes-payment-global"


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
    if (parts.scheme != "https" or not host or parts.username is not None
            or parts.password is not None or parts.path or parts.query or parts.fragment
            or not host.isascii() or any(c in host for c in "\\%")):
        raise PaymentPolicyError("HTTPS origin required")
    authority = f"[{host}]" if ":" in host else host
    canonical = "https://" + authority + (f":{port}" if port not in (None, 443) else "")
    if canonical != value or port == 0:
        raise PaymentPolicyError("Canonical HTTPS origin required")
    return value


@dataclass(frozen=True)
class PaymentMetadata:
    handle: str
    payment_scope: str
    merchant_origin: str | None
    frame_origins: tuple[str, ...]

    @property
    def available(self) -> bool:
        # Deliberately not a feature flag. Only a proven browser-side primitive
        # may replace this refusal; config/model input cannot enable global fill.
        return False


def onepassword_payment_metadata(item: Mapping, configured_vault: str = "") -> PaymentMetadata | None:
    """Consume only op list metadata; ignore fields, masked numbers and titles.

    CREDIT_CARD is op JSON's category enum (CLI spelling is 'Credit Card').
    Untagged cards retain origin-bound policy; an unbound card grants no scope.
    This helper does not change the installed Login-only backend listing.
    """
    if item.get("category") != "CREDIT_CARD":
        return None
    item_id = item.get("id")
    vault = item.get("vault")
    vault_id = vault.get("id") if isinstance(vault, Mapping) else None
    vault_id = vault_id or configured_vault
    if not isinstance(item_id, str) or not item_id or ":" in item_id:
        return None
    if not isinstance(vault_id, str) or ":" in vault_id:
        return None
    handle = f"op:{vault_id}:{item_id}" if vault_id else f"op:{item_id}"
    tags = item.get("tags")
    global_scope = isinstance(tags, list) and GLOBAL_PAYMENT_TAG in tags
    urls = item.get("urls")
    urls = urls if isinstance(urls, list) else []
    ordered = [u for u in urls if isinstance(u, Mapping) and u.get("primary") is True]
    ordered += [u for u in urls if isinstance(u, Mapping) and u.get("primary") is not True]
    origins = []
    for url in ordered:
        href = url.get("href")
        if not isinstance(href, str):
            continue
        try:
            parts = urlsplit(href)
            # Website paths are metadata; discard paths, never credentials.
            if parts.username is not None or parts.password is not None:
                continue
            host = parts.hostname or ""
            host = f"[{host}]" if ":" in host else host
            origin = parts.scheme + "://" + host
            if parts.port not in (None, 443):
                origin += f":{parts.port}"
            origin = https_origin(origin)
        except ValueError:
            continue
        if origin not in origins:
            origins.append(origin)
    return PaymentMetadata(handle, "global" if global_scope else "origin_bound",
                           origins[0] if origins else None, tuple(origins[1:]))


@dataclass(frozen=True)
class PaymentDocument:
    frame_id: str
    document_id: str
    session_id: str
    page_id: str
    origin: str
    # Browser-authoritative frame identity AND origin, immediate parent first.
    ancestors: tuple[tuple[str, str], ...]
    lifecycle_revision: int
    field_stamps: tuple[tuple[str, str], ...]  # canonical role, opaque stamp
    attached: bool = True


@dataclass(frozen=True)
class PaymentEnvelope:
    operation_id: str
    page_id: str
    session_id: str
    merchant_frame_id: str
    merchant_document_id: str
    merchant_origin: str
    lifecycle_revision: int
    # Complete inspection universe, including documents with NO payment fields.
    documents: tuple[PaymentDocument, ...]
    selected_frame_ids: tuple[str, ...]

    def validate(self) -> None:
        https_origin(self.merchant_origin)
        identities = (self.operation_id, self.page_id, self.session_id,
                      self.merchant_frame_id, self.merchant_document_id)
        if not all(isinstance(i, str) and i for i in identities):
            raise PaymentPolicyError("Missing envelope identity")
        if type(self.lifecycle_revision) is not int or self.lifecycle_revision < 0:
            raise PaymentPolicyError("Invalid lifecycle revision")
        if not isinstance(self.documents, tuple) or not isinstance(self.selected_frame_ids, tuple):
            raise PaymentPolicyError("Immutable envelope required")
        if not all(isinstance(i, str) and i for i in self.selected_frame_ids):
            raise PaymentPolicyError("Invalid selection identity")
        frames = set()
        candidates = []
        roles = set()
        for doc in self.documents:
            if not isinstance(doc, PaymentDocument):
                raise PaymentPolicyError("Invalid document identity")
            https_origin(doc.origin)
            if (doc.attached is not True or doc.page_id != self.page_id
                    or not all(isinstance(i, str) and i for i in
                               (doc.frame_id, doc.document_id, doc.session_id, doc.page_id))
                    or doc.frame_id in frames
                    or type(doc.lifecycle_revision) is not int or doc.lifecycle_revision < 0
                    or not isinstance(doc.ancestors, tuple) or not isinstance(doc.field_stamps, tuple)):
                raise PaymentPolicyError("Invalid document identity")
            frames.add(doc.frame_id)
            if doc.frame_id == self.merchant_frame_id:
                if (doc.ancestors or doc.origin != self.merchant_origin
                        or doc.document_id != self.merchant_document_id or doc.session_id != self.session_id):
                    raise PaymentPolicyError("Merchant document mismatch")
            else:
                if not doc.ancestors or doc.ancestors[-1] != (self.merchant_frame_id, self.merchant_origin):
                    raise PaymentPolicyError("Ancestry mismatch")
            seen_ancestors = {doc.frame_id}
            for ancestor in doc.ancestors:
                if not isinstance(ancestor, tuple) or len(ancestor) != 2:
                    raise PaymentPolicyError("Invalid ancestry")
                frame_id, origin = ancestor
                https_origin(origin)
                if not isinstance(frame_id, str) or not frame_id or frame_id in seen_ancestors:
                    raise PaymentPolicyError("Invalid ancestry")
                seen_ancestors.add(frame_id)
            stamps = set()
            for field in doc.field_stamps:
                if not isinstance(field, tuple) or len(field) != 2:
                    raise PaymentPolicyError("Invalid field stamp")
                role, stamp = field
                if not isinstance(role, str) or role not in {
                    "card_number", "cvc", "exp_month", "exp_year", "cardholder_name", "billing_postal_code"
                }:
                    raise PaymentPolicyError("Unknown payment field")
                if role in roles or not isinstance(stamp, str) or not stamp or stamp in stamps:
                    raise PaymentPolicyError("Ambiguous payment fields")
                roles.add(role)
                stamps.add(stamp)
            if doc.field_stamps:
                candidates.append(doc.frame_id)
        by_frame = {doc.frame_id: doc for doc in self.documents}
        if self.merchant_frame_id not in frames:
            raise PaymentPolicyError("Missing merchant document")
        for doc in self.documents:
            for index, (frame_id, origin) in enumerate(doc.ancestors):
                parent = by_frame.get(frame_id)
                if parent is None or parent.origin != origin or parent.ancestors != doc.ancestors[index + 1:]:
                    raise PaymentPolicyError("Incomplete ancestry")
        if (not candidates or len(set(self.selected_frame_ids)) != len(self.selected_frame_ids)
                or set(candidates) != set(self.selected_frame_ids)):
            raise PaymentPolicyError("Selection mismatch")
        # No split writes without a genuine multi-renderer transaction.
        if len(candidates) != 1:
            raise PaymentPolicyError("Split payment documents unsupported")
        if "card_number" not in roles:
            raise PaymentPolicyError("Missing payment field set")

    def confirmation_payload(self) -> dict:
        self.validate()
        selected = set(self.selected_frame_ids)
        return {"operation_id": self.operation_id, "merchant_origin": self.merchant_origin,
                "target_origins": sorted({d.origin for d in self.documents if d.frame_id in selected})}


def validate_confirmation(confirmed: PaymentEnvelope, current: PaymentEnvelope, *, accepted: bool) -> None:
    """Snapshot equality only, BEFORE resolution. Does not close the CDP race."""
    if accepted is not True:
        raise PaymentPolicyError("Payment declined")
    confirmed.validate()
    current.validate()
    if confirmed != current:
        raise PaymentPolicyError("Consent envelope changed")


def require_global_fill_capability() -> None:
    """Unconditional gate: no caller can turn snapshot validation into injection."""
    raise PaymentPolicyError("Global payment fill unavailable: atomic browser primitive not accepted")
