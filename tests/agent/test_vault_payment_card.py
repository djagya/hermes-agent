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
    # The layout the native fill accepts: one document, or merchant plus ONE hosted provider.
    from agent.vault_payment_policy import PAYMENT_PSP_ORIGINS
    from tools.browser_supervisor_payment import _MAX_CARD_DOCUMENTS
    assert item["supported_layout"] == "single_or_split_one_provider"
    assert item["max_card_documents"] == _MAX_CARD_DOCUMENTS == 4
    assert item["hosted_origins"] == list(PAYMENT_PSP_ORIGINS)
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


@pytest.mark.platforms("posix")
def test_card_revision_is_the_op_version_from_the_listing_and_from_the_revealed_item(tmp_path, monkeypatch):
    """Design v2 A8: the prompt binds the listing's ``version``; the reveal reports the version it read, so an
    item edited between the two (version 3 -> 4) is told apart. Synthetic op, synthetic card."""
    script = tmp_path / "op"
    listed = {**{k: v for k, v in CARD.items() if k != "fields"}, "version": 3}
    script.write_text(
        "#!/usr/bin/env python3\nimport json,sys\na=sys.argv[1:]\n"
        f"listed={listed!r}\ncard={dict(CARD, version=4)!r}\n"
        "if a[:2]==['item','list']:print(json.dumps([listed]))\n"
        "elif a[:2]==['item','get'] and '--reveal' in a:print(json.dumps(card))\nelse:sys.exit(2)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-rev"))
    backend = OnePasswordLoginBackend({"binary_path": str(script)})
    backend._service_token = "synthetic-service-token"
    meta = backend.get_meta("op:vault:card")
    assert meta.revision == "op.version=3" and "revision" not in meta.to_dict()
    card, revision = backend.resolve_payment_secret("op:vault:card")
    assert revision == "op.version=4" and card["card_number"] == CARD["fields"][0]["value"]


_AT = "2026-11-09T10:00:00Z"


@pytest.mark.parametrize("listed, revealed, verdict", [
    ({"version": 3, "updated_at": _AT}, {"version": 3, "updated_at": _AT}, "match"),
    ({"version": 3, "updated_at": _AT}, {"version": 4, "updated_at": _AT}, "changed"),
    ({"updated_at": _AT}, {"updated_at": _AT}, "match"),  # no version anywhere: updated_at decides
    ({"updated_at": _AT}, {"updated_at": "2026-11-09T10:00:05Z"}, "changed"),
    # Codex A2 r3 #4: a version on one side only is not compared away (same parts on both sides or nothing)
    ({"version": 3, "updated_at": _AT}, {"updated_at": _AT}, "unavailable"),
    ({"updated_at": _AT}, {"version": 4, "updated_at": _AT}, "unavailable"),
    ({"version": 3}, {"updated_at": _AT}, "unavailable"),  # nothing both carry
    ({}, {}, "unavailable"),  # missing never equals missing
    ({"version": True, "updated_at": "a;b"}, {"version": True, "updated_at": "a;b"}, "unavailable"),
])
def test_op_revision_compares_only_metadata_both_responses_carry(listed, revealed, verdict):
    from agent.vault_backends.onepassword import _item_revision
    from agent.vault_store import revision_verdict

    assert revision_verdict(_item_revision({**CARD, **listed}), _item_revision({**CARD, **revealed})) == verdict
    assert all(f["value"] not in _item_revision(CARD) for f in CARD["fields"])


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("meta_version, revealed, verdict, binding", [
    (3, 3, "match", "version"),
    (3, 4, "changed", "version"),  # edited between the prompt and the reveal, same updated_at
    (None, None, "match", "updated_at_only"),  # op gives no version anywhere: updated_at, reported as weaker
])
def test_a_versionless_listing_binds_the_version_from_a_metadata_lookup(tmp_path, monkeypatch, meta_version,
                                                                      revealed, verdict, binding):
    """Codex A2 r3 #4 / design v2 A8: the listing has no ``version``, so the prompt-side revision comes from one
    ``op item get`` without ``--reveal``; the reveal is then compared on the same parts. Synthetic op and card."""
    from agent.vault_store import revision_binding, revision_verdict

    script = tmp_path / "op"
    meta = {k: v for k, v in CARD.items() if k != "fields"}
    listed = {**meta, "updated_at": _AT}
    looked = {**listed, **({"version": meta_version} if meta_version is not None else {})}
    card = {**CARD, "updated_at": _AT, **({"version": revealed} if revealed is not None else {})}
    script.write_text(
        "#!/usr/bin/env python3\nimport json,sys\na=sys.argv[1:]\n"
        f"open({str(script.with_suffix('.log'))!r},'a').write(json.dumps(a)+'\\n')\n"
        f"listed={listed!r}\nlooked={looked!r}\ncard={card!r}\n"
        "if a[:2]==['item','list']:print(json.dumps([listed]))\n"
        "elif a[:2]==['item','get'] and '--reveal' in a:print(json.dumps(card))\n"
        "elif a[:2]==['item','get']:print(json.dumps(looked))\nelse:sys.exit(2)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-lookup"))
    backend = OnePasswordLoginBackend({"binary_path": str(script)})
    backend._service_token = "synthetic-service-token"
    listed_meta = backend.get_meta("op:vault:card")
    assert listed_meta.revision == f"op.updated_at={_AT}"
    bound = backend.payment_revision(listed_meta)
    _card, read = backend.resolve_payment_secret("op:vault:card")
    assert revision_verdict(bound, read) == verdict and revision_binding(bound) == binding
    calls = [json.loads(line) for line in script.with_suffix(".log").read_text().splitlines()]
    assert ["item", "get", "card", "--vault", "vault", "--format", "json"] in calls  # the lookup never reveals
    assert all(f["value"] not in bound for f in CARD["fields"])
