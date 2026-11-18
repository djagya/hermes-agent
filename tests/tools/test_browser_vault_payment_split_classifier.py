"""Round 5 of the card-holder classifier lane (hint promotion, attribute-only holders, expiry reach): each rule needs
positive evidence in the field's own text. The helpers (``_token``, ``_pipeline``) are the split suite's."""

from __future__ import annotations

import pytest

from tests.tools.test_browser_vault_payment_split import CARD, _CVC, _EXPIRY, _NUMBER, _PAN, _pipeline, _token

# ---- Round 5: hint promotion, attribute-only holders and expiry reach on positive evidence ----------------------
@pytest.mark.parametrize("control, token", [
    # Codex P1 (a): a letter the ASCII folding drops is meaningful own text, not silence ("Номер телефона").
    ({"name": "f1 ", "label": "Номер телефона", "hint": "Card number"}, None),
    ({"name": "f1 ", "label": "电话号码", "hint": "Card number"}, None),
    ({"name": "f1 ", "label": "رقم الهاتف", "hint": "Card number"}, None),
    ({"name": "f1 ", "label": "Telefonnummer ß", "hint": "Card number"}, None),
    ({"name": " ", "hint": "Номер карты Card number"}, None), ({"name": " ", "hint": "Card number 卡号"}, None),
    # ... and an accent the folding keeps as the base letter is no such letter.
    ({"name": "f1 ", "label": "  ••••", "hint": "Numéro de carte"}, "cc-number"),
    # Codex P1 (b): the whole hint is one card role plus approved neutral words, nothing else.
    ({"name": " ", "hint": "Card number and PIN"}, None), ({"name": " ", "hint": "Card number recovery code"}, None),
    ({"name": " ", "hint": "Card number or phone"}, None), ({"name": " ", "hint": "Card number for refunds"}, None),
    ({"name": " ", "hint": "Expiry date of your passport"}, None), ({"name": " ", "hint": "CVC for the gift card"}, None),
    ({"name": " ", "hint": "Name on card and signature"}, None),
    ({"name": " ", "hint": "Card number"}, "cc-number"), ({"name": " ", "hint": "Card number (16 digits)"}, "cc-number"),
    ({"name": " ", "hint": "Enter card number"}, "cc-number"), ({"name": " ", "hint": "Enter your credit card number"}, "cc-number"),
    ({"name": " ", "hint": "Security code (3 digits on the back)"}, "cc-csc"),
    ({"name": " ", "hint": "Expiry date (MM/YY)"}, "cc-exp"), ({"name": " ", "hint": "Name on card"}, "cc-name"),
    ({"name": " ", "hint": "Cardholder name"}, "cc-name"),
])
def test_round_5_a_hint_names_a_silent_field_only_as_one_role_plus_neutral_words(control, token):
    assert _token(**control) == token


@pytest.mark.parametrize("hint_attrs, texts", [
    ({"title": "Card number and PIN"}, {}), ({"title": "Card number recovery code"}, {}),
    ({"aria-labelledby": "grp"}, {"grp": "Card number and PIN"}),
    ({"aria-labelledby": "grp"}, {"grp": "Card number recovery code"}),
])
def test_round_5_a_hint_beside_other_words_never_receives_the_card_number(hint_attrs, texts):
    # Codex P1: the group may describe the PIN input; beside the expiry and CVC the form stays incomplete (base too).
    suspect = {"name": "f1", "id": "f1", "label": "", "attrs": hint_attrs}
    assert _pipeline([suspect, _EXPIRY, _CVC], texts) == ("Incomplete or ambiguous card fields", None)
    _fills, writes = _pipeline([suspect, _NUMBER, _EXPIRY, _CVC], texts)
    assert len(writes) == 3 and all(element != "f1" for element, _value in writes)


@pytest.mark.parametrize("label", ["Номер телефона", "电话号码", "رقم الهاتف"])
def test_round_5_a_non_latin_labelled_input_titled_as_the_card_number_never_receives_it(label):
    suspect = {"name": "f1", "id": "f1", "label": label, "attrs": {"title": "Card number"}}
    assert _pipeline([suspect, _EXPIRY, _CVC]) == ("Incomplete or ambiguous card fields", None)
    _fills, writes = _pipeline([suspect, _NUMBER, _EXPIRY, _CVC])
    assert len(writes) == 3 and all(element != "f1" for element, _value in writes)


def test_round_5_a_hint_of_one_role_still_names_a_field_with_no_own_text():
    hosted = {"name": "", "id": "f1", "label": "", "attrs": {"title": "Card number (16 digits)"}}
    _fills, writes = _pipeline([hosted, _EXPIRY, _CVC])
    assert ["f1", _PAN] in writes and len(writes) == 3


_ALLOWED_PLACEHOLDERS = ("Max Mustermann", "Erika Mustermann", "John Doe", "Jane Doe", "Jean Dupont", "Mario Rossi",
                         "Juan Pérez", "Jan Jansen", "Vorname Nachname", "First Last")


@pytest.mark.parametrize("label", ["Recipient", "Ship to", "Security question", "PIN", "Notes", "Delivery instructions",
                                   "Maiden name", "Nickname", "Display name", "Mother's name", "Max Mustermann PIN",
                                   "Security question Max Mustermann"])
def test_round_5_a_holder_named_only_by_its_attribute_beside_another_purpose_stays_unclassified(label):
    # Opus M1, Codex P2: name/id "card-holder" with a visible label naming something else is that something else.
    for attrs in ({"name": "card-holder"}, {"name": "card_holder"}, {"name": "card-holder x"}, {"name": " card-holder"}):
        assert _token(label=label, **attrs) is None, (label, attrs)


@pytest.mark.parametrize("label", ["", "  ", "Name", "Full name", "Vorname Nachname", *_ALLOWED_PLACEHOLDERS])
def test_round_5_a_holder_named_only_by_its_attribute_beside_an_empty_neutral_or_placeholder_label(label):
    assert _token(name="card-holder", label=label) == "cc-name"
    assert _token(name="card-holder x", label=label) == "cc-name"


@pytest.mark.parametrize("label", ["Card holder Max Mustermann", "Karteninhaber Max Mustermann",
                                   "Card holder Jane Doe", "Cardholder name Max Mustermann",
                                   "Name of the card holder Erika Mustermann", "Card holder First Last"])
def test_round_5_the_holder_phrase_followed_by_a_name_placeholder_is_the_holder(label):
    assert _token(label=label) == "cc-name" and _token(name="card-holder", label=label) == "cc-name"


@pytest.mark.parametrize("label", ["Card holder Recipient", "Card holder Max Mustermann PIN", "Card holder Security question"])
def test_round_5_the_holder_phrase_followed_by_another_word_is_not_the_holder(label):
    assert _token(label=label) is None and _token(name="card-holder", label=label) is None


@pytest.mark.parametrize("label", ["Recipient", "Ship to", "Security question", "PIN", "Notes"])
def test_round_5_attribute_only_holder_fields_beside_another_purpose_never_receive_the_holder_name(label):
    fields = [{"name": "card-holder", "id": "x", "label": label}, {"name": "", "id": "card-holder", "label": label}]
    for field in fields:
        fills, writes = _pipeline([_NUMBER, _EXPIRY, _CVC, field])
        assert sorted(f["token"] for f in fills) == ["cc-csc", "cc-exp", "cc-number"]
        assert len(writes) == 3 and all(element not in ("x", "card-holder") for element, _value in writes)


@pytest.mark.parametrize("holder", [
    {"name": "", "id": "card-holder", "placeholder": "Max Mustermann"},
    {"name": "card-holder", "id": "x", "label": "Full name"},
    {"name": "", "id": "x", "label": "Card holder Max Mustermann"},
    {"name": "", "id": "x", "label": "Karteninhaber Max Mustermann"},
])
def test_round_5_the_unzer_form_and_the_incident_variants_are_still_written(holder):
    fills, writes = _pipeline([_NUMBER, _EXPIRY, _CVC, holder])
    assert [f["value"] for f in fills if f["token"] == "cc-name"] == [CARD["cardholder_name"]]
    assert len(writes) == 4 and [holder["id"], CARD["cardholder_name"]] in writes


@pytest.mark.parametrize("control", [
    {"name": "visaValidUntil"}, {"name": "visaExpiry"}, {"name": "visa-valid-until"}, {"label": "Visa valid until"},
    {"name": "visaExpMonth", "type": "select"}, {"name": "visaExpYear", "type": "select"},
    {"label": "ESTA valid until"}, {"label": "Licence valid until"}, {"label": "Subscription valid until"},
    {"label": "Offer valid until"}, {"name": "validUntil"}, {"label": "Expires"}, {"label": "Gültigkeitsdatum"},
    {"name": "licenceExpiryDate"},
])
def test_round_5_the_wider_expiry_reach_without_a_card_word_is_never_the_card_expiry(control):
    assert _token(**control) is None


@pytest.mark.parametrize("control, token", [
    ({"label": "Expiry date (Visa)"}, "cc-exp"), ({"label": "Visa CVC"}, "cc-csc"), ({"label": "Visa expiry date"}, "cc-exp"),
    ({"name": "ccExp"}, "cc-exp"), ({"name": "encryptedExpiryDate"}, "cc-exp"), ({"name": "encryptedExpiryMonth"}, "cc-exp-month"),
    ({"label": "Card valid until"}, "cc-exp"), ({"label": "Kreditkarte gültig bis"}, "cc-exp"),
    ({"label": "Credit card expires"}, "cc-exp"), ({"name": "cardExpiryDate"}, "cc-exp"),
    ({"label": "Visa card valid until"}, "cc-exp"), ({"label": "Karte Gültigkeitsdatum"}, "cc-exp"),
])
def test_round_5_card_evidence_and_the_base_wording_keep_the_expiry(control, token):
    assert _token(**control) == token


@pytest.mark.parametrize("field", [
    {"name": "visaValidUntil", "id": "v"}, {"name": "visa-valid-until", "id": "v"}, {"name": "visaExpiry", "id": "v"},
    {"name": "", "id": "v", "label": "ESTA valid until"}, {"name": "", "id": "v", "label": "Visa valid until"},
])
def test_round_5_a_visa_validity_field_never_receives_the_card_expiry_nor_makes_the_form_ambiguous(field):
    # Codex P2: beside a genuine expiry it made a complete checkout refuse as ambiguous; in place of it, it took it.
    fills, writes = _pipeline([_NUMBER, _EXPIRY, _CVC, field])
    assert sorted(f["token"] for f in fills) == ["cc-csc", "cc-exp", "cc-number"]
    assert len(writes) == 3 and all(element != "v" for element, _value in writes)
    assert _pipeline([_NUMBER, field, _CVC]) == ("Missing card expiry fields", None)
