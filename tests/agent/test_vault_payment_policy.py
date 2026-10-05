"""Production native metadata/origin policy. Browser consent is tested live."""

import pytest

from agent.vault_payment_policy import (
    PaymentPolicyError,
    https_origin,
    onepassword_payment_metadata,
)


def test_metadata_uses_only_qualified_card_identity():
    item = {
        "category": "CREDIT_CARD",
        "id": "card",
        "vault": {"id": "vault"},
        "urls": [{"href": "https://bank.example"}],
        "tags": ["hermes-payment-global"],
        "fields": [{"id": "ccnum", "value": "NEVER-COPY-SENTINEL"}],
    }
    meta = onepassword_payment_metadata(item)
    assert meta.handle == "op:vault:card" and "NEVER-COPY" not in repr(meta)
    assert onepassword_payment_metadata({**item, "category": "LOGIN"}) is None
    assert onepassword_payment_metadata({**item, "id": "injected:card"}) is None
    assert onepassword_payment_metadata({**item, "vault": {}}) is None
    assert (
        onepassword_payment_metadata({**item, "vault": {}}, "configured").handle
        == "op:configured:card"
    )


def test_only_canonical_https_origin_is_accepted():
    assert https_origin("https://merchant.example") == "https://merchant.example"
    for origin in (
        "http://merchant.example",
        "data:text/plain,dummy",
        "file:///dummy",
        "chrome://settings",
        "null",
        "",
        "https://merchant.example/checkout",
        "https://merchant.example:443",
        "https://user:pass@merchant.example",
        "https://merchant.example:0",
    ):
        with pytest.raises(PaymentPolicyError):
            https_origin(origin)
