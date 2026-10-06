"""1Password card discovery and qualified reveal via the real CLI subprocess seam."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.vault_backends.onepassword import OnePasswordLoginBackend
from agent.vault_payment_card import parse_onepassword_card

CARD = {
    "id": "card",
    "category": "CREDIT_CARD",
    "vault": {"id": "vault"},
    "title": "Synthetic Visa",
    "fields": [
        {"id": "ccnum", "value": "4242424242424242"},
        {"id": "cvv", "value": "837"},
        {"id": "expiry", "value": "203507"},
        {"id": "cardholder", "value": "Synthetic User"},
    ],
}


def test_card_parser_rejects_wrong_identity_ambiguity_and_concealed_values():
    result = parse_onepassword_card(CARD, item_id="card", vault_id="vault")
    assert result["exp_month"] == "07" and result["exp_year"] == "2035"
    for changed in (
        {**CARD, "id": "other"},
        {**CARD, "vault": {"id": "other"}},
        {**CARD, "category": "LOGIN"},
        {**CARD, "fields": CARD["fields"] + [CARD["fields"][0]]},
        {
            **CARD,
            "fields": [{"id": f["id"], "value": "concealed"} for f in CARD["fields"]],
        },
    ):
        with pytest.raises(ValueError):
            parse_onepassword_card(changed, item_id="card", vault_id="vault")


@pytest.mark.platforms("posix")
def test_manager_card_metadata_and_native_resolution_are_separate(
    tmp_path, monkeypatch
):
    script = tmp_path / "op"
    metadata = {k: v for k, v in CARD.items() if k != "fields"}
    script.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n"
        "a=sys.argv[1:]\np=Path(__file__).with_suffix('.log')\n"
        "with p.open('a') as f:f.write(json.dumps(a)+'\\n')\n"
        f"metadata={metadata!r}\ncard={CARD!r}\n"
        "if a[:2]==['item','list']:print(json.dumps([metadata]))\n"
        "elif a[:2]==['item','get'] and '--reveal' in a and a[a.index('--vault')+1]=='vault':"
        "print(json.dumps(card))\nelse:sys.exit(2)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
    backend = OnePasswordLoginBackend({"binary_path": str(script)})
    backend._service_token = "synthetic-service-token"
    from tools.browser_vault_tool import browser_vault_list

    with patch("agent.vault_backends.enabled_backends", return_value=[backend]):
        listed = browser_vault_list()
    item = json.loads(listed)["items"][0]
    assert item["handle"] == "op:vault:card" and item["kind"] == "payment"
    assert item["available"] and item["payment_scope"] == "confirmed_page"
    assert all(f["value"] not in listed for f in CARD["fields"])
    calls = [
        json.loads(line) for line in script.with_suffix(".log").read_text().splitlines()
    ]
    assert all(call[:2] == ["item", "list"] for call in calls)
    resolved = backend.resolve_secret(item["handle"])
    assert resolved["card_number"] == CARD["fields"][0]["value"]
    with pytest.raises(ValueError):
        backend.resolve_secret("op:card")
    calls = [
        json.loads(line) for line in script.with_suffix(".log").read_text().splitlines()
    ]
    assert all(f["value"] not in json.dumps(calls) for f in CARD["fields"])
