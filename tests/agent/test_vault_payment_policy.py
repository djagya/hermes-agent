"""Disabled payment capability: exact metadata policy and snapshot consent contracts.

Synthetic metadata only. These tests deliberately do not claim browser atomicity.
"""
from dataclasses import FrozenInstanceError, replace

import pytest

from agent.vault_payment_policy import (
    PaymentDocument, PaymentEnvelope, PaymentPolicyError,
    onepassword_payment_metadata, require_global_fill_capability, validate_confirmation,
)


def test_metadata_policy_is_exact_and_cannot_enable_fill():
    base = {"category": "CREDIT_CARD", "id": "card", "vault": {"id": "vault"},
            "urls": [{"href": "https://merchant.example/checkout", "primary": True},
                     {"href": "https://psp.example/fields"}],
            "fields": [{"id": "ccnum", "value": "NEVER-COPY-SENTINEL"}],
            "additional_information": "NEVER-COPY-SENTINEL"}
    for tags in ([], ["hermes-payment-global-ish"], ["Hermes-payment-global"],
                 ["hermes-payment-global "], "hermes-payment-global", None):
        meta = onepassword_payment_metadata({**base, "tags": tags})
        assert meta is not None
        assert meta.payment_scope == "origin_bound"
        assert meta.merchant_origin == "https://merchant.example"
        assert meta.frame_origins == ("https://psp.example",)
        assert meta.handle == "op:vault:card" and not meta.available
        assert "NEVER-COPY" not in repr(meta)
    global_card = onepassword_payment_metadata({**base, "tags": ["hermes-payment-global"], "urls": []})
    assert global_card is not None
    assert global_card.payment_scope == "global" and global_card.merchant_origin is None
    assert not global_card.available
    unbound = onepassword_payment_metadata({**base, "urls": []})
    assert unbound is not None
    assert unbound.payment_scope == "origin_bound" and not unbound.available
    assert onepassword_payment_metadata({**base, "category": "LOGIN", "tags": ["hermes-payment-global"]}) is None
    with pytest.raises(PaymentPolicyError, match="unavailable"):
        require_global_fill_capability()


def test_consent_envelope_binds_whole_scope_and_decline_never_authorizes_resolution():
    merchant = PaymentDocument("top", "top-doc", "page-session", "page", "https://arbitrary.example", (), 1, ())
    psp = PaymentDocument("psp", "psp-doc", "oopif-session", "page", "https://stripe.example",
                          (("top", merchant.origin),), 2,
                          (("card_number", "stamp-number"), ("cvc", "stamp-cvc")))
    sibling = replace(psp, frame_id="sibling", document_id="sibling-doc", field_stamps=())
    envelope = PaymentEnvelope("one-use", "page", "page-session", "top", "top-doc", merchant.origin,
                               3, (merchant, psp, sibling), ("psp",))
    assert envelope.confirmation_payload() == {
        "operation_id": "one-use", "merchant_origin": merchant.origin,
        "target_origins": [psp.origin]}
    validate_confirmation(envelope, envelope, accepted=True)
    with pytest.raises(FrozenInstanceError):
        setattr(envelope, "page_id", "replacement")
    with pytest.raises(PaymentPolicyError, match="declined"):
        validate_confirmation(envelope, envelope, accepted=False)
    # Top-level controls (no iframe) also form a valid, single selected document.
    top_fields = replace(merchant, field_stamps=psp.field_stamps)
    replace(envelope, documents=(top_fields,), selected_frame_ids=("top",)).validate()
    changes = [
        replace(envelope, operation_id="replay"),
        replace(envelope, page_id="stale-tab"),
        replace(envelope, session_id="stale-session"),
        replace(envelope, lifecycle_revision=4),
        replace(envelope, documents=(merchant, replace(psp, document_id="navigated"), sibling)),
        replace(envelope, documents=(merchant, replace(psp, origin="https://new-psp.example"), sibling)),
        replace(envelope, documents=(merchant, replace(psp, attached=False), sibling)),
        replace(envelope, documents=(merchant, replace(psp, session_id="reattached"), sibling)),
        replace(envelope, documents=(merchant, replace(psp, ancestors=()), sibling)),
        replace(envelope, documents=(merchant, replace(psp, field_stamps=(("card_number", "new"),)), sibling)),
        replace(envelope, documents=(merchant, psp, replace(sibling, field_stamps=(("card_number", "dup"),)))),
        replace(envelope, documents=(merchant, psp, sibling, replace(sibling, frame_id="new-frame"))),
        replace(envelope, documents=(merchant, replace(psp, field_stamps=(("card_number", "n"),)),
                                    replace(sibling, field_stamps=(("cvc", "c"),))),
                selected_frame_ids=("psp", "sibling")),
        replace(envelope, documents=(merchant, replace(psp, ancestors=(("missing-parent", "https://parent.example"),
                                                                     ("top", merchant.origin))), sibling)),
    ]
    for current in changes:
        with pytest.raises(PaymentPolicyError):
            validate_confirmation(envelope, current, accepted=True)
    for origin in ("http://merchant.example", "data:text/plain,dummy", "file:///tmp/dummy", "chrome://settings",
                   "chrome-extension://dummy", "null", "", "https://merchant.example/checkout",
                   "https://user:pass@merchant.example", "https://merchant.example:0"):
        with pytest.raises(PaymentPolicyError):
            replace(envelope, merchant_origin=origin).validate()
    # Snapshot validation never removes the unconditional capability gate.
    with pytest.raises(PaymentPolicyError, match="unavailable"):
        require_global_fill_capability()
