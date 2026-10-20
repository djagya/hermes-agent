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


def test_opt_in_providers_need_config_and_metadata_only_narrows(monkeypatch):
    from agent import vault_payment_policy as policy

    monkeypatch.setattr(policy, "enabled_providers", lambda: policy.DEFAULT_PROVIDERS)
    assert "https://payments.worldpay.com" not in policy.effective_frame_origins()
    with pytest.raises(PaymentPolicyError):  # card metadata cannot widen trust to an unreviewed origin
        policy.effective_frame_origins(("https://payments.worldpay.com",))
    assert policy.effective_frame_origins(("https://js.stripe.com",)) == ("https://js.stripe.com",)

    monkeypatch.undo()
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"vault": {"payment_providers": ["worldpay-hpp", "no-such-provider"]}})
    assert "https://payments.worldpay.com" in policy.effective_frame_origins()
    assert "https://www.paypal.com" not in policy.effective_frame_origins()
    assert all(https_origin(o) == o for p in policy.PAYMENT_PROVIDERS for o in p.origins)


@pytest.mark.parametrize("url, admitted", [
    ("https://www.paypal.com/smart/card-field?token=x", True),
    ("https://www.paypal.com/smart/card-field/sub", True),
    ("https://www.paypal.com/smart/buttons", False),
    ("https://www.paypal.com/signin", False),
    ("https://www.paypal.com/smart/card-fieldx", False),
])
def test_shared_origin_provider_admits_only_its_card_field_paths(url, admitted):
    from agent.vault_payment_policy import provider_for_origin

    assert provider_for_origin("https://www.paypal.com").admits(url) is admitted
