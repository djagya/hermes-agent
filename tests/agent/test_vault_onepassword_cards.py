"""1Password Credit Card items as ``kind=payment`` vault handles.

Contracts:
1. Listing is metadata-only: a card's number/CVC never appear in the list output, and a card is fillable
   only when the user bound it to an https website IN 1Password (primary website = merchant page, other
   https websites = payment-provider frames). A card with no https website is listed as unavailable.
   Card handles are vault-qualified (``op:<vault>:<item>``) exactly like Login handles.
2. Resolution runs server-side through the real subprocess path (``op item get <id> --vault <vault>
   --format json --reveal``,
   card values only on the child's stdout, never argv) and maps the 1Password card template onto the
   vault's canonical PAYMENT_FIELDS. Missing required values are reported by FIELD NAME only.
All card data here is synthetic (Visa test PAN).
"""

from __future__ import annotations

import json
import os
import stat
from unittest.mock import patch

import pytest

from agent.vault_backends import unlock as unlock_mod
from agent.vault_backends.onepassword import OnePasswordLoginBackend, card_payload

_PAN, _CVC = "4111111111111111", "737"

# Stand-in `op` for the two commands the backend uses. The session token must arrive through the child
# env (OP_SESSION), and every argv is logged beside the script so the test can prove no card value is argv.
_FAKE_OP = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "op.log"), "a") as log:
    log.write(json.dumps(argv) + "\n")
if os.environ.get("OP_SESSION") != "TOK":
    sys.stderr.write("not signed in\n"); sys.exit(1)
if argv[:2] == ["item", "list"]:
    assert argv[argv.index("--categories") + 1] == "Login,Credit Card"
    print(json.dumps([
        {"id": "card1", "title": "Travel Visa", "category": "CREDIT_CARD", "additional_information": "**** 1111",
         "vault": {"id": "v1", "name": "Private"},
         "urls": [{"href": "https://www.sbb.ch/en/buying", "primary": True}, {"href": "https://pay.datatrans.com/upp"},
                  {"href": "http://insecure.example"}]},
        {"id": "card2", "title": "Unbound card", "category": "CREDIT_CARD", "additional_information": "**** 4444"},
        {"id": "login1", "title": "Example", "category": "LOGIN", "additional_information": "jane@example.com",
         "urls": [{"href": "https://example.com/login"}]},
        {"id": "note1", "title": "Note", "category": "SECURE_NOTE"},
    ])); sys.exit(0)
if argv[:2] == ["item", "get"] and argv[2] == "card1" and argv[3:5] == ["--vault", "v1"]:
    print(json.dumps({"id": "card1", "category": "CREDIT_CARD", "fields": [
        {"id": "cardholder", "label": "cardholder name", "value": "A User"},
        {"id": "ccnum", "label": "number", "value": "4111 1111 1111 1111"},
        {"id": "cvv", "label": "verification number", "value": "737"},
        {"id": "expiry", "label": "expiry date", "type": "MONTH_YEAR", "value": "202907"},
    ]})); sys.exit(0)
if argv[:2] == ["item", "get"] and argv[2] == "card2":
    print(json.dumps({"id": "card2", "category": "CREDIT_CARD", "fields": [
        {"id": "ccnum", "label": "number", "value": "5555555555554444"}]})); sys.exit(0)
sys.exit(2)
'''

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake op is a shebang script; the backend under test is host-agnostic")


@pytest.fixture
def op_backend(tmp_path, monkeypatch):
    exe = tmp_path / "op"
    exe.write_text(_FAKE_OP, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    unlock_mod.lock()
    backend = OnePasswordLoginBackend({"enabled": True, "binary_path": str(exe)})
    backend._service_token = ""  # the profile may hold one; this test drives the session-token path
    assert unlock_mod.store_session_token("onepassword", "TOK", unlock_mod.begin_unlock("onepassword"))
    yield backend, tmp_path / "op.log"
    unlock_mod.lock()


def test_cards_list_as_payment_handles_bound_only_to_their_own_https_websites(op_backend):
    backend, log = op_backend
    from tools.browser_vault_tool import browser_vault_list

    with patch("agent.vault_backends.enabled_backends", return_value=[backend]):
        raw = browser_vault_list()
    items = {i["handle"]: i for i in json.loads(raw)["items"]}
    assert items["op:v1:card1"]["kind"] == "payment" and items["op:v1:card1"]["available"] is True
    assert items["op:v1:card1"]["origin"] == "https://www.sbb.ch"
    assert items["op:v1:card1"]["frame_origins"] == ["https://pay.datatrans.com"]  # plaintext website dropped
    assert items["op:card2"]["available"] is False and items["op:card2"]["origin"] is None
    assert "two_factor" not in items["op:v1:card1"]
    assert items["op:login1"]["kind"] == "login" and "op:note1" not in items
    assert "1111" not in raw and "4444" not in raw  # not even op's masked number reaches the agent
    assert not any("get" in json.loads(line) for line in log.read_text().splitlines())


def test_card_resolves_server_side_to_canonical_payment_fields(op_backend):
    backend, log = op_backend
    card = backend.resolve_secret("op:v1:card1")
    assert card == {"card_number": _PAN, "cvc": _CVC, "cardholder_name": "A User", "exp_month": "7", "exp_year": "2029"}
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["item", "get", "card1", "--vault", "v1", "--format", "json", "--reveal"] in calls
    assert all(_PAN not in line and _CVC not in line for line in log.read_text().splitlines())
    with pytest.raises(RuntimeError) as exc:
        backend.resolve_secret("op:card2")
    assert "5555" not in str(exc.value) and "exp_month" in str(exc.value) and "cvc" in str(exc.value)


def test_unbound_card_is_refused_before_any_secret_read(op_backend):
    backend, log = op_backend
    from tools.browser_vault_tool import browser_vault_fill

    with patch("agent.vault_backends.base.enabled_backends", return_value=[backend]), \
         patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
        out = json.loads(browser_vault_fill("op:card2", task_id="t"))
    assert out["success"] is False and out["error_type"] == "no_origin"
    consent.assert_not_called()
    assert not any(json.loads(line)[:2] == ["item", "get"] for line in log.read_text().splitlines())


@pytest.mark.parametrize("expiry,expected", [("202907", ("7", "2029")), ("07/29", ("7", "2029")),
                                             ("7/2031", ("7", "2031")), ("2029-11", ("11", "2029"))])
def test_card_expiry_formats_map_to_month_and_four_digit_year(expiry, expected):
    out = card_payload([{"id": "expiry", "value": expiry}])
    assert (out["exp_month"], out["exp_year"]) == expected


def test_card_with_invalid_expiry_is_missing_the_fields_not_guessed():
    assert "exp_month" not in card_payload([{"id": "expiry", "value": "13/29"}])
