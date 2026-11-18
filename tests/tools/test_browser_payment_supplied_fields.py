"""Non-secret card fields Sera supplies (D-0360 a; 2026-11-18).

Under a managed payment lock Sera cannot type after the fill, so the card fill takes the holder name and the billing
postcode as optional arguments: the saved card wins, a supplied value is used only where the card has none, an unusable
value refuses the fill before anything is read, the approval card says Sera supplied them (never their values), and the
result says per written token where its value came from. Synthetic cards and pages only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools import browser_supervisor_payment as sp
from tools import browser_supervisor_payment_results as rs
from tools import browser_vault_payment as bvp
from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection

MERCHANT = "https://shop.example"
CARD = {"card_number": "4242424242424242", "exp_month": "07", "exp_year": "2035", "cvc": "837"}
HOLDER, POSTCODE = "Grace Hopper", "SW1A 1AA"


def _row(index, name, label, form=0):
    return {"index": index, "autocomplete": "", "name": name + " ", "label": label, "type": "text", "formIndex": form,
            "maxLength": None}


_FORM = [_row(0, "ccnum", "Card number"), _row(1, "exp", "Expiry date"), _row(2, "cvc", "CVC")]
_NAME, _ZIP = _row(3, "holder", "Name on card"), _row(4, "zip", "Postal code")


def _inspection(rows, blank=None):
    classified = rs._classified(rows)
    controls = rs._card_of(classified, True)
    document = PaymentDocument("f", "s", "o", MERCHANT, controls,
                               left_for_user=rs._left_for_user(classified, controls, True), blank=blank)
    return PaymentInspection("op", "page", "target", MERCHANT, (document,), (), 1e12)


class Flow:
    """One card fill over fakes: what the owner's card showed, what was written, what the card read and the window saw."""

    def __init__(self, monkeypatch, rows=None, card=None):
        rows = rows or _FORM + [_NAME, _ZIP]
        self.inspection = _inspection(rows, blank=frozenset(r["index"] for r in rows))  # an empty form
        self.card, self.reads, self.written, self.prompts, self.windows = dict(CARD, **(card or {})), 0, [], [], []
        flow = self

        class Supervisor:
            def inspect_payment(self, *a, **k):
                return flow.inspection

            def payment_is_current(self, inspected):
                return True

            def commit_payment(self, inspected, fills, **k):
                flow.written = [dict(f) for f in fills]
                return {"success": True, "filled_fields": len(fills)}

            def discard_payment(self, inspected):
                pass

        def resolve(handle):
            flow.reads += 1
            return dict(flow.card)

        def consent(message, description, **k):
            flow.prompts.append((description, k.get("payment")))
            return "accept"

        monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
        monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: False)
        monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
        monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda task_id, surface=None: None)
        monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", consent)
        monkeypatch.setattr(bvp, "_register_card_redaction",
                            lambda secret, supervisor=None: self.windows.append(dict(secret)) or ("k", "id"))
        monkeypatch.setattr("tools.browser_payment_card_values.seal_payment_window", lambda *a: None)
        monkeypatch.setattr("tools.browser_payment_card_values.withdraw_payment_window", lambda *a: None)
        monkeypatch.setattr("tools.browser_payment_done.register_dialog_card_values", lambda *a, **k: None)
        self.meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None,
                                    payment_scope="confirmed_page", payment_frame_origins=())
        self.backend = SimpleNamespace(name="local", resolve_secret=resolve, get_meta=lambda handle: self.meta)

    def fill(self, **supplied):
        return json.loads(bvp.fill_payment(self.backend, self.meta, "task", **supplied))

    def values(self):
        return {f["token"]: f["value"] for f in self.written}


@pytest.fixture
def flow(monkeypatch):
    return Flow(monkeypatch)


def test_the_saved_value_wins_over_a_supplied_one(monkeypatch):
    flow = Flow(monkeypatch, card={"cardholder_name": "Ada Lovelace", "billing_postal_code": "8001"})
    out = flow.fill(holder_name=HOLDER, billing_postal_code=POSTCODE)
    assert out["success"] is True, out
    assert flow.values()["cc-name"] == "Ada Lovelace" and flow.values()["postal-code"] == "8001"
    assert out["field_sources"]["cc-name"] == "vault" and out["field_sources"]["postal-code"] == "vault"
    assert HOLDER not in json.dumps(out) and POSTCODE not in json.dumps(out)


def test_a_supplied_value_is_written_where_the_card_has_none(flow):
    out = flow.fill(holder_name=HOLDER, billing_postal_code=POSTCODE)
    assert out["success"] is True, out
    assert flow.values()["cc-name"] == HOLDER and flow.values()["postal-code"] == POSTCODE
    assert flow.values()["cc-number"] == "4242424242424242"
    assert out["field_sources"] == {"cc-csc": "vault", "cc-exp": "vault", "cc-name": "agent", "cc-number": "vault",
                                    "postal-code": "agent"}
    # the result says where each came from, never the values; the form is complete (both fields were written)
    assert HOLDER not in json.dumps(out) and POSTCODE not in json.dumps(out)
    assert out["form_complete"] is True and out["skipped_optional"] == []


def test_each_optional_field_takes_its_own_source(monkeypatch):
    flow = Flow(monkeypatch, card={"cardholder_name": "Ada Lovelace"})
    out = flow.fill(billing_postal_code=POSTCODE)
    assert flow.values()["cc-name"] == "Ada Lovelace" and flow.values()["postal-code"] == POSTCODE
    assert out["field_sources"]["cc-name"] == "vault" and out["field_sources"]["postal-code"] == "agent"


def test_without_arguments_the_fill_is_as_before(flow):
    out = flow.fill()
    assert out["success"] is True and "cc-name" not in flow.values() and "postal-code" not in flow.values()
    assert out["skipped_optional"] == ["cc-name", "postal-code"]
    assert set(out["field_sources"].values()) == {"vault"} and "cc-name" not in out["field_sources"]
    assert "supplied by Sera" not in flow.prompts[0][0]


def test_a_supplied_holder_joins_the_cards_payment_window_like_a_saved_one(flow):
    flow.fill(holder_name=HOLDER, billing_postal_code=POSTCODE)
    assert flow.windows[0]["cardholder_name"] == HOLDER  # scrubbed from browser and log output afterwards


@pytest.mark.parametrize("args, field", [
    ({"holder_name": "A"}, "holder_name"),
    ({"holder_name": "x" * 61}, "holder_name"),
    ({"holder_name": "Ada 2nd"}, "holder_name"),
    ({"holder_name": "Ada <script>"}, "holder_name"),
    ({"holder_name": "Ada; DROP"}, "holder_name"),
    ({"holder_name": "..'-/"}, "holder_name"),
    ({"holder_name": 42}, "holder_name"),
    ({"billing_postal_code": "80"}, "billing_postal_code"),
    ({"billing_postal_code": "8" * 11}, "billing_postal_code"),
    ({"billing_postal_code": "80#1"}, "billing_postal_code"),
    ({"billing_postal_code": "-8001"}, "billing_postal_code"),
    ({"billing_postal_code": 8001}, "billing_postal_code"),
    ({"holder_name": "Ada Lovelace", "billing_postal_code": "8001!"}, "billing_postal_code"),
])
def test_an_unusable_supplied_value_refuses_before_anything_is_read(flow, args, field):
    out = flow.fill(**args)
    assert out["success"] is False and out["error_type"] == "payment_field_value_invalid" and out["field"] == field
    assert flow.reads == 0 and flow.prompts == [] and flow.written == []
    assert not any(str(v) in json.dumps(out) for v in args.values() if len(str(v)) > 4)  # the value is never echoed


@pytest.mark.parametrize("value, clean", [
    ("Ada Lovelace", "Ada Lovelace"), ("  Ada   Lovelace ", "Ada Lovelace"), ("Zoë Müller-O'Neil", "Zoë Müller-O'Neil"),
    ("Zoë O’Neil", "Zoë O'Neil"), ("J. R. R. Tolkien", "J. R. R. Tolkien"), ("Li", "Li"), ("A/B Co.", "A/B Co."),
    ("x" * 60, "x" * 60), ("", ""), (None, ""), ("   ", ""),
])
def test_usable_holder_names(value, clean):
    assert bvp._clean_holder(value) == clean


@pytest.mark.parametrize("value, clean", [
    ("8001", "8001"), ("SW1A 1AA", "SW1A 1AA"), ("  sw1a   1aa ", "sw1a 1aa"), ("10115", "10115"),
    ("D-10115", "D-10115"), ("123", "123"), ("9" * 10, "9" * 10), ("", ""), (None, ""),
])
def test_usable_postcodes(value, clean):
    assert bvp._clean_postcode(value) == clean


def test_the_owners_card_says_sera_supplied_them_and_never_shows_the_values(flow):
    flow.fill(holder_name=HOLDER, billing_postal_code=POSTCODE)
    description, payment = flow.prompts[0]
    assert "Holder name and postcode supplied by Sera" in description
    assert HOLDER not in description and POSTCODE not in description and HOLDER not in json.dumps(payment)
    assert payment["supplied_fields"] == ["holder_name", "billing_postal_code"]


def test_the_card_names_only_the_field_that_was_supplied(flow):
    flow.fill(billing_postal_code=POSTCODE)
    assert "Postcode supplied by Sera" in flow.prompts[0][0] and "Holder name" not in flow.prompts[0][0]
    flow.fill(holder_name=HOLDER)
    assert "Holder name supplied by Sera" in flow.prompts[1][0] and "postcode" not in flow.prompts[1][0].lower()


def test_the_telegram_html_card_carries_the_line_and_the_gateways_copy_keeps_it(flow):
    from tools.payment_consent_prompt import PaymentPromptFacts, render_html

    flow.fill(holder_name=HOLDER)
    facts = PaymentPromptFacts.from_dict(flow.prompts[0][1])
    assert facts.supplied_fields == ("holder_name",)
    html = render_html(facts, budget_utf16=4096)
    assert "Holder name supplied by Sera" in html and HOLDER not in html
    # an older facts record without the key still reads (nothing supplied)
    older = {k: v for k, v in flow.prompts[0][1].items() if k != "supplied_fields"}
    assert PaymentPromptFacts.from_dict(older).supplied_fields == ()
    with pytest.raises(Exception):
        PaymentPromptFacts.from_dict({**older, "extra": 1})
    with pytest.raises(Exception):
        PaymentPromptFacts.from_dict({**flow.prompts[0][1], "supplied_fields": ["cardholder"]})


# --- the next text under a lock -----------------------------------------------------------------------------------


def _report(rows, card, locked, blank):
    inspection = _inspection(rows, blank=frozenset(blank))
    fills = bvp._document_fills(inspection, card)
    return bvp._form_report({"success": True}, inspection, fills, locked)


def test_a_locked_browser_is_first_told_to_fill_again_with_the_missing_non_secret_fields():
    out = _report(_FORM + [_NAME, _ZIP], dict(CARD), True, {3, 4})
    assert out["form_complete"] is False and out["fields_left_empty"]["card_fields"] == ["cc-name", "postal-code"]
    assert "fill again with holder_name" in out["next"] and "billing_postal_code" in out["next"]
    assert "payment_close" not in out["next"] and "Do not press Pay" in out["next"]
    one = _report(_FORM + [_NAME], dict(CARD), True, {3})
    assert "holder_name" in one["next"] and "billing_postal_code" not in one["next"]


def test_other_empty_fields_keep_the_close_reopen_type_path_after_the_fill_again_text():
    other = _row(5, "x9", "Kontoinhaber Vorname")
    out = _report(_FORM + [_NAME, other], dict(CARD), True, {3, 5})
    assert out["next"].index("holder_name") < out["next"].index("payment_close")
    only_other = _report(_FORM + [other], dict(CARD), True, {5})
    assert "payment_close" in only_other["next"] and "fill again with" not in only_other["next"]


def test_an_unlocked_browser_keeps_the_type_it_yourself_text():
    out = _report(_FORM + [_NAME], dict(CARD), False, {3})
    assert "yourself" in out["next"] and "fill again with" not in out["next"]


def test_a_supplied_value_that_was_written_leaves_nothing_to_fill_again(flow):
    flow.inspection = _inspection(_FORM + [_NAME, _ZIP], blank=frozenset({3, 4}))
    out = flow.fill(holder_name=HOLDER, billing_postal_code=POSTCODE)
    assert out["form_complete"] is True and "next" not in out


# --- the tool's schema and handler -------------------------------------------------------------------------------


def test_the_tool_schema_offers_both_arguments_and_the_handler_passes_them(monkeypatch):
    from tools import browser_vault_tool as tool

    props = tool.BROWSER_VAULT_SCHEMA["parameters"]["properties"]
    assert props["holder_name"]["type"] == "string" and props["billing_postal_code"]["type"] == "string"
    seen = {}
    monkeypatch.setattr(tool, "browser_vault_fill", lambda **k: seen.update(k) or json.dumps({"success": True}))
    tool._handle_vault_inner({"action": "fill", "handle": "h", "holder_name": HOLDER, "billing_postal_code": POSTCODE},
                             task_id="t")
    assert seen["holder_name"] == HOLDER and seen["billing_postal_code"] == POSTCODE
    seen.clear()
    tool._handle_vault_inner({"action": "fill", "handle": "h"}, task_id="t")
    assert "holder_name" not in seen and "billing_postal_code" not in seen  # passed only when given
