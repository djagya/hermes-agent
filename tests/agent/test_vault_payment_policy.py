"""Production native metadata/origin policy. Browser consent is tested live."""

import pytest

from agent.vault_payment_policy import (
    PaymentPolicyError,
    coverage_hint,
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


_SHOP = "https://shop.example"


def _tree(*frames):
    return (("main", "", _SHOP + "/checkout", _SHOP, ""),
            *((f"f{n}", "", url, origin, "main") for n, (url, origin) in enumerate(frames)))


@pytest.mark.parametrize("frame,allowed,expected", [
    (("https://www.computop-paygate.com/paySSL.aspx?x=1", "https://www.computop-paygate.com"), (),
     "card frames on https://www.computop-paygate.com: Computop"),
    (("https://secure.pay1.de/client-api/js/v1/payone_iframe.html", "https://secure.pay1.de"), (),
     "https://secure.pay1.de is reviewed provider payone-hosted-iframes, not enabled"),
    (("https://js.mollie.com/v1/component/card", "https://js.mollie.com"), (),
     "https://js.mollie.com is reviewed provider mollie-components, not enabled in vault.payment_providers"),
    (("https://www.paypal.com/smart/buttons<b>?q=1", "https://www.paypal.com"), ("https://www.paypal.com",),
     "https://www.paypal.com/smart/buttonsb is not a paypal-card-fields card-field document"),
    (("https://www.paypal.com/smart/card-field/x", "https://www.paypal.com"), ("https://www.paypal.com",), ""),
    (("https://ads.example/frame", "https://ads.example"), (), ""),
])
def test_coverage_hint_names_the_provider_level_cause(frame, allowed, expected):
    hint = coverage_hint(_tree(frame), _SHOP, allowed)
    assert (expected in hint) if expected else hint == ""
    assert "<" not in hint and "?" not in hint and "ads.example" not in hint


def test_an_unreadable_config_turns_destination_consent_off_through_the_real_loader(tmp_path, monkeypatch):
    """Codex B r1 #3: the loader serves last-known-good for a broken config.yaml; that is not the current choice."""
    import os

    from agent.vault_payment_policy import destination_policy
    from hermes_cli import config as config_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_mod._LOAD_CONFIG_CACHE.clear()
    path = tmp_path / "config.yaml"
    path.write_text("vault:\n  payment_destination_consent: true\n", encoding="utf-8")
    enabled, generation = destination_policy()
    assert enabled is True and generation
    broken = tmp_path / ".config.yaml.new"
    broken.write_text("vault: [unclosed\n", encoding="utf-8")
    os.replace(broken, path)  # a new inode: the cache sees a changed file
    assert destination_policy() == (False, "")


def test_an_unreadable_config_drops_opt_in_providers_through_the_real_loader(tmp_path, monkeypatch):
    """A broken config.yaml served as last-known-good never keeps an opt-in provider enabled."""
    import os

    from agent.vault_payment_policy import OPT_IN_PROVIDERS, enabled_providers
    from hermes_cli import config as config_mod

    name = OPT_IN_PROVIDERS[0].name
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_mod._LOAD_CONFIG_CACHE.clear()
    path = tmp_path / "config.yaml"
    path.write_text(f"vault:\n  payment_providers: [{name}]\n", encoding="utf-8")
    assert name in {p.name for p in enabled_providers()}
    broken = tmp_path / ".config.yaml.new"
    broken.write_text("vault: [unclosed\n", encoding="utf-8")
    os.replace(broken, path)
    assert name not in {p.name for p in enabled_providers()}
