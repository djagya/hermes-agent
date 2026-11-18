"""Split card checkouts (Datatrans Secure Fields, Stripe split elements) without a browser.

The live receipts in test_browser_vault_payment_live.py drive real Chromium; these pin the policy:
which document sets are accepted, how fills are grouped, what a partial write reports, and which
values are registered for redaction.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from agent.vault_payment_policy import PAYMENT_PSP_ORIGINS, PaymentPolicyError, https_origin
from tools import browser_supervisor_payment as sp
from tools.browser_supervisor_payment import (
    PaymentDocument,
    PaymentInspection,
    _card_documents,
    _explained_card_documents,
)

MERCHANT = "https://shop.example"
DATATRANS = "https://pay.datatrans.com"
STRIPE = "https://js.stripe.com"
CARD = {"card_number": "4242424242424242", "exp_month": "07", "exp_year": "2035", "cvc": "837",
        "cardholder_name": "Ada Lovelace"}


def _controls(*tokens):
    return tuple(classify_checkout_control(LoginControl.from_dict({"index": n, "autocomplete": t}))
                 for n, t in enumerate(tokens))


def _candidate(origin, *tokens, frame="f"):
    return (f"{frame}-{origin}", f"s-{frame}", f"o-{frame}", origin, _controls(*tokens))


def _inspection(*documents):
    return PaymentInspection("op", "page", "target", MERCHANT, tuple(documents), (), 1e12)


def test_reviewed_psp_origins_are_canonical():
    assert {STRIPE, DATATRANS, "https://checkout.pci.shopifyinc.com"} == set(PAYMENT_PSP_ORIGINS)
    assert all(https_origin(o) == o for o in PAYMENT_PSP_ORIGINS)


@pytest.mark.parametrize("label, token", [
    ("Expiration date (MM / YY)", "cc-exp"), ("Expiry date", "cc-exp"),
    ("Issue date (MM / YY)", None), ("Start date MM/YY", None), ("Valid from", None), ("Issue number", None),
])
def test_a_maestro_issue_or_start_date_is_never_the_expiry(label, token):
    control = classify_checkout_control(LoginControl.from_dict({"index": 0, "autocomplete": "off", "label": label}))
    assert (control.token if control else None) == token


def test_one_document_is_accepted_as_before():
    docs = _card_documents([_candidate(STRIPE, "cc-number", "cc-exp", "cc-csc")], MERCHANT)
    assert [d.origin for d in docs] == [STRIPE]


def test_datatrans_split_number_and_cvc_frames_with_merchant_expiry():
    docs = _card_documents([
        _candidate(MERCHANT, "cc-exp-month", "cc-exp-year", frame="page"),
        _candidate(DATATRANS, "cc-number", frame="number"),
        _candidate(DATATRANS, "cc-csc", frame="cvc"),
    ], MERCHANT)
    inspection = _inspection(*docs)
    assert inspection.target_origins == (MERCHANT, DATATRANS)
    assert inspection.frame_origin == DATATRANS
    prompt = inspection.prompt()
    assert prompt["target_origin"] == f"{MERCHANT}, {DATATRANS}"
    assert "card fields split across 3 documents, each field once" in prompt["checks"]


@pytest.mark.parametrize("candidates, message", [
    ([], "Missing, ambiguous or split"),
    ([_candidate(DATATRANS, "cc-number", frame="a"), _candidate(STRIPE, "cc-csc", "cc-exp", frame="b")],
     "split across payment providers"),
    ([_candidate(DATATRANS, "cc-number", "cc-exp", frame="a"), _candidate(DATATRANS, "cc-number", "cc-csc", frame="b")],
     "Incomplete or ambiguous"),
    ([_candidate(DATATRANS, "cc-number", "cc-exp", frame="a")], "Incomplete or ambiguous"),
    ([_candidate(MERCHANT, "cc-number", "cc-csc", frame="a"), _candidate(DATATRANS, "cc-exp-month", frame="b")],
     "Missing card expiry"),
    ([_candidate(MERCHANT, "cc-exp", "cc-exp-month", frame="a"), _candidate(DATATRANS, "cc-number", "cc-csc", frame="b"),
      _candidate(DATATRANS, "cc-exp-year", frame="c")], "Ambiguous card expiry"),
    ([_candidate(DATATRANS, t, frame=str(n)) for n, t in enumerate(
        ("cc-number", "cc-csc", "cc-exp-month", "cc-exp-year", "cc-name"))], "Missing, ambiguous or split"),
])
def test_ambiguous_or_cross_provider_splits_are_refused(candidates, message):
    with pytest.raises(PaymentPolicyError, match=message):
        _card_documents(candidates, MERCHANT)


def test_fills_are_grouped_per_document_and_every_document_needs_fields():
    i = _inspection(PaymentDocument("a", "s", "o", MERCHANT, _controls("cc-exp")),
                    PaymentDocument("b", "s", "o", DATATRANS, _controls("cc-number")))
    groups = sp._fills_by_document(i, [{"index": 0, "value": "07/35", "document": 0},
                                       {"index": 0, "value": "4242", "document": 1}])
    assert groups == [[{"index": 0, "value": "07/35"}], [{"index": 0, "value": "4242"}]]
    for bad in ([{"index": 0, "value": "x", "document": 2}], [{"index": 0, "value": "x", "document": True}],
                [{"index": 0, "value": "x"}]):
        with pytest.raises(PaymentPolicyError):
            sp._fills_by_document(i, bad)
    # Only a document holding nothing but optional fields (a holder frame, a card without a holder
    # name) may go unwritten.
    holder = _inspection(PaymentDocument("h", "s", "o", MERCHANT, _controls("cc-name")),
                         PaymentDocument("b", "s", "o", DATATRANS, _controls("cc-number")))
    assert sp._fills_by_document(holder, [{"index": 0, "value": "4242", "document": 1}])[0] == []


@pytest.fixture(autouse=True)
def _no_settle_wait(monkeypatch):
    monkeypatch.setattr(sp, "_SETTLE_SECONDS", 0)


class _FakeSupervisor(sp.PaymentSupervisionMixin):
    """Commit results per document; ``retained`` maps a token to its state after settling (default held)."""

    def __init__(self, results, retained=None):
        self.results, self.writes, self.released = list(results), [], False
        self.retained = retained or {}

    async def _payment_is_current(self, i):
        return True

    async def _payment_scope_current(self, i, documents):
        return True  # pinned-scope checks are exercised over a fake CDP in _PayPalPage below

    async def _commit_payment_document(self, d, fills, deadline_ms=None, strict=False, paced=None, **_kw):
        self.deadlines = getattr(self, "deadlines", []) + [deadline_ms]
        self.writes.append((d.origin, fills))
        return self.results.pop(0)

    async def _payment_retention(self, i, d):
        fills = next(f for origin, f in self.writes if origin == d.origin)
        if self.retained.get(d.origin) == "gone":
            return None
        return [{"token": f.get("token", ""), "state": self.retained.get(f.get("token"), "held"),
                 "length": 0 if self.retained.get(f.get("token")) == "empty" else len(f["value"])} for f in fills]

    async def _shown_documents(self, i):
        return {d.frame_id for d in i.documents}

    async def _release_payment_documents(self, i):
        self.released = True


def _split_inspection():
    return _inspection(PaymentDocument("a", "s1", "o1", MERCHANT, ()), PaymentDocument("b", "s2", "o2", DATATRANS, ()))


def test_split_commit_writes_each_document_once_and_sums_fields():
    sup, i = _FakeSupervisor([{"success": True, "filled_fields": 1}, {"success": True, "filled_fields": 2}]), _split_inspection()
    fills = [{"index": 0, "value": "07/35", "document": 0}, {"index": 0, "value": "n", "document": 1},
             {"index": 1, "value": "c", "document": 1}]
    assert asyncio.run(sup._commit_payment(i, fills)) == {"success": True, "filled_fields": 3,
                                                           "retention_verified": True}
    assert [origin for origin, _ in sup.writes] == [MERCHANT, DATATRANS] and sup.released
    assert asyncio.run(sup._commit_payment(i, fills))["error_type"] == "payment_consent_consumed"


def test_a_later_document_failure_reports_the_partial_fill_and_never_retries():
    sup = _FakeSupervisor([{"success": True, "filled_fields": 1},
                           {"success": False, "error_type": "payment_fields_changed"}])
    i = _split_inspection()
    out = asyncio.run(sup._commit_payment(i, [{"index": 0, "value": "a", "document": 0},
                                             {"index": 0, "value": "b", "document": 1}]))
    assert out == {"success": False, "error_type": "payment_fields_changed", "filled_fields": 1,
                   "partially_filled": True}
    assert len(sup.writes) == 2 and i.consumed and sup.released


def test_fill_payment_names_every_target_tags_fills_and_redacts_only_card_secrets(monkeypatch):
    from tools import browser_vault_payment as bvp

    docs = _card_documents([_candidate(MERCHANT, "cc-exp-month", "cc-exp-year", "cc-name", frame="page"),
                            _candidate(DATATRANS, "cc-number", frame="number"),
                            _candidate(DATATRANS, "cc-csc", frame="cvc")], MERCHANT)
    inspection = _inspection(*docs)
    committed, registered, consent_text = [], [], []

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            return True

        def commit_payment(self, i, fills, strict=False):
            committed.extend(fills)
            i.consumed = True
            return {"success": True, "filled_fields": len(fills)}

        def discard_payment(self, i):
            pass

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda task_id, surface=None: None)
    monkeypatch.setattr("agent.redact.register_vault_redaction_value", registered.append)
    windows = []
    from agent import redact

    real_register = redact.register_card_window

    def spy_register(key, digits=(), words=()):  # the seal needs the window to exist: record, then open it for real
        windows.append((digits, words))
        return real_register(key, digits=digits, words=words)

    monkeypatch.setattr("agent.redact.register_card_window", spy_register)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent",
                        lambda *args, **k: consent_text.append(args[1]) or "accept")
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: dict(CARD), get_meta=lambda handle: meta)

    out = json.loads(bvp.fill_payment(backend, meta, "task"))
    assert out["success"] and out["target_origin"] == DATATRANS and out["target_origins"] == [MERCHANT, DATATRANS]
    assert f"{MERCHANT}, {DATATRANS}" in consent_text[0]
    assert sorted((f["document"], f["token"]) for f in committed) == [
        (0, "cc-exp-month"), (0, "cc-exp-year"), (0, "cc-name"), (1, "cc-number"), (2, "cc-csc")]
    # r8: no card value goes to the exact-match registry; the payment window takes the number and CVC (masked
    # by length until the release) and the holder, never the expiry
    assert windows == [(("4242424242424242", "837"), ("Ada Lovelace",))] and not registered, (windows, registered)


def test_supervisor_attach_errors_never_reach_the_result(monkeypatch):
    from tools import browser_vault_payment as bvp

    def boom(task_id):
        raise RuntimeError("ws://127.0.0.1:9222/devtools/browser/secret-token")

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", boom)
    raw = bvp.fill_payment(SimpleNamespace(), SimpleNamespace(), "task")
    out = json.loads(raw)
    assert out["error_type"] == "supervisor_required" and "secret-token" not in raw
    assert "Nothing was read or written" in out["error"]  # a typed refusal names its next step


@pytest.mark.parametrize("case", ["supervisor_none", "no_origin", "page_changed"])
def test_early_refusals_carry_a_next_step(monkeypatch, case):
    from tools import browser_vault_payment as bvp

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return _four_hosted_documents()

        def payment_is_current(self, i):
            return False

        def discard_payment(self, i):
            pass

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor",
                        lambda task_id: None if case == "supervisor_none" else Supervisor())
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "accept")
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None, payment_frame_origins=PAYMENT_PSP_ORIGINS,
                           payment_scope="" if case == "no_origin" else "confirmed_page")
    out = json.loads(bvp.fill_payment(SimpleNamespace(name="local", get_meta=lambda handle: meta), meta, "task"))
    expected = {"supervisor_none": "supervisor_required", "no_origin": "no_origin", "page_changed": "payment_page_changed"}
    assert out["success"] is False and out["error_type"] == expected[case]
    assert len(out.get("error", "")) > 40, out


@pytest.mark.parametrize("control, token", [
    ({"label": "Kartenprüfnummer"}, "cc-csc"), ({"label": "Gültig bis (MM/JJ)"}, "cc-exp"),
    ({"label": "Gültig ab"}, None), ({"label": "Name des Karteninhabers *"}, "cc-name"),
    ({"label": "Numéro de carte"}, "cc-number"), ({"label": "Fecha de caducidad"}, "cc-exp"),
    ({"name": "card-ccv"}, "cc-csc"),  # Unzer UI v1
    ({"name": "cvc", "type": "password"}, "cc-csc"),  # Unzer UI v2 masks the CVC
    ({"label": "Card number", "type": "password"}, None),  # a masked input is never the number
    ({"name": "CARDHOLDER_EMAIL"}, None),  # Nexi Build: not the holder name
])
def test_localized_and_provider_labels(control, token):
    c = classify_checkout_control(LoginControl.from_dict({"index": 0, "autocomplete": "off", "type": "text", **control}))
    assert (c.token if c else None) == token


# Unzer UI v1's hosted holder input as the inspection closure describes it (name + " " + id, the placeholder as the
# only label text; the parent page's "Karteninhaber" label is outside the frame). It was classified None, so the
# fill left the holder frame out and did not list it as skipped.
_UNZER_HOLDER = {"index": 0, "autocomplete": "off", "name": " card-holder", "label": "  Max Mustermann",
                 "type": "text", "formIndex": None, "maxLength": 60}


@pytest.mark.parametrize("control, token", [
    (_UNZER_HOLDER, "cc-name"),
    ({"name": "card_holder"}, "cc-name"), ({"label": "Card Holder Name"}, "cc-name"), ({"name": "cardholder"}, "cc-name"),
    ({"name": "cardHolder"}, "cc-name"), ({"label": "Karteninhaber"}, "cc-name"),
    # Generic contact names, an e-mail and login inputs stay out, separated spelling or not.
    ({"name": "customer-name"}, None), ({"label": "Full name"}, None), ({"label": "First name"}, None),
    ({"label": "Last name"}, None), ({"name": "CARDHOLDER_EMAIL"}, None), ({"label": "card holder email"}, None),
    ({"name": "card-holder", "type": "email"}, None), ({"name": "username"}, None),
    ({"name": "password", "type": "password"}, None),
    ({"name": "card-holder", "type": "password"}, None),  # a masked input is only ever the CVC
    ({"name": "card-holder", "type": "select"}, None),  # a select is never the holder
])
def test_a_separated_card_holder_name_is_the_holder(control, token):
    c = classify_checkout_control(LoginControl.from_dict({"index": 0, "autocomplete": "off", "type": "text", **control}))
    assert (c.token if c else None) == token


_UNZER = "https://payment.unzer.com"


def _unzer_frames(holder=_UNZER_HOLDER, extra=()):
    """Four Unzer UI v1 cardframe documents, one input each, as raw inspection rows; ``extra``: more holder rows."""
    rows = {"number": [{**_UNZER_HOLDER, "name": " card-number", "label": " ", "maxLength": 23}],
            "expiry": [{**_UNZER_HOLDER, "name": " card-expiry-date", "label": "  MM / YY", "maxLength": 7}],
            "cvc": [{**_UNZER_HOLDER, "name": " card-ccv", "label": " ", "maxLength": 4}],
            "holder": [holder, *({**holder, "index": n} for n, _ in enumerate(extra, 1))]}
    return rows


def _unzer_candidates(frames, origins=None):
    """Candidates as the inspection collects them: only a document with card controls is one."""
    origins = origins or {}
    out = [(f"f-{k}", f"s-{k}", f"o-{k}", origins.get(k, _UNZER), sp._card_controls(sp._bounded_description(raw)))
           for k, raw in frames.items()]
    return [c for c in out if c[4]]


class _PerFrameSupervisor(_FakeSupervisor):
    """_FakeSupervisor keyed by frame, not origin: every Unzer UI v1 document is on the same origin."""

    async def _commit_payment_document(self, d, fills, deadline_ms=None, strict=False, paced=None, **_kw):
        self.by_frame = {**getattr(self, "by_frame", {}), d.frame_id: fills}
        return await super()._commit_payment_document(d, fills, deadline_ms, strict, paced, **_kw)

    async def _payment_retention(self, i, d):
        answer = [{"token": f.get("token", ""), "state": self.retained.get(f.get("token"), "held"),
                   "length": 0 if self.retained.get(f.get("token")) == "empty" else len(f["value"])}
                  for f in self.by_frame[d.frame_id]]
        self.checked = getattr(self, "checked", []) + [a["token"] for a in answer]
        return answer


def test_four_unzer_frames_plan_the_holder_once_and_check_that_it_held():
    docs = _card_documents(_unzer_candidates(_unzer_frames()), MERCHANT)
    inspection = _inspection(*docs)
    assert [[c.token for c in d.controls] for d in docs] == [["cc-number"], ["cc-exp"], ["cc-csc"], ["cc-name"]]
    assert inspection.prompt()["fields"] == ["cc-csc", "cc-exp", "cc-name", "cc-number"]

    from tools import browser_vault_payment as bvp

    fills = bvp._document_fills(inspection, CARD)
    holder = [f for f in fills if f["token"] == "cc-name"]
    assert holder == [{"index": 0, "token": "cc-name", "value": "Ada Lovelace", "document": 3}]
    assert sorted(f["token"] for f in fills) == ["cc-csc", "cc-exp", "cc-name", "cc-number"]

    sup = _PerFrameSupervisor([{"success": True, "filled_fields": 1}] * 4)
    assert asyncio.run(sup._commit_payment(inspection, fills)) == {"success": True, "filled_fields": 4,
                                                                    "retention_verified": True}
    assert sorted(sup.checked) == ["cc-csc", "cc-exp", "cc-name", "cc-number"]  # the holder's retention was read
    assert [fill["token"] for _o, w in sup.writes for fill in w].count("cc-name") == 1

    # A holder the provider cleared after settling fails the fill like any other field.
    sup = _PerFrameSupervisor([{"success": True, "filled_fields": 1}] * 4, retained={"cc-name": "empty"})
    out = asyncio.run(sup._commit_payment(_inspection(*_card_documents(_unzer_candidates(_unzer_frames()), MERCHANT)),
                                          fills))
    assert out["error_type"] == "payment_fields_not_retained" and out["field_retention"]["cc-name"] == "empty"


@pytest.mark.parametrize("frames, origins, message", [
    # two holder inputs in the holder frame
    (_unzer_frames(extra=[1]), None, "Incomplete or ambiguous card fields"),
    # the holder in a frame from another hosted origin
    (_unzer_frames(), {"holder": "https://other-psp.example"}, "split across payment providers"),
    # a second holder on the merchant page: five documents, over Unzer UI v1's four
    ({**_unzer_frames(), "page": [_UNZER_HOLDER]}, {"page": MERCHANT}, "Missing, ambiguous or split"),
])
def test_an_ambiguous_or_foreign_unzer_holder_is_refused(frames, origins, message):
    with pytest.raises(PaymentPolicyError, match=message):
        _card_documents(_unzer_candidates(frames, origins), MERCHANT)


def _token(**raw):
    c = classify_checkout_control(LoginControl.from_dict({"index": 0, "autocomplete": "", "type": "text", **raw}))
    return c.token if c else None


@pytest.mark.parametrize("control, token", [
    # A holder cue beside another purpose keeps that purpose's token (the base classification), or stays None.
    ({"label": "Card holder ZIP code"}, "postal-code"), ({"label": "Card holder postal code"}, "postal-code"),
    ({"label": "Card holder address"}, "address-line1"), ({"label": "Card holder street"}, "address-line1"),
    ({"label": "Cardholder address"}, "address-line1"),  # was cc-name on base too
    ({"label": "Card holder billing address line 2"}, "address-line2"), ({"label": "Card holder city"}, "address-level2"),
    ({"label": "Card holder state"}, "address-level1"), ({"label": "Card holder country"}, "country-name"),
    ({"label": "Card holder expiry date"}, "cc-exp"), ({"name": "card-holder", "label": "Expiry month"}, "cc-exp-month"),
    ({"label": "Card holder phone", "type": "tel"}, None), ({"name": "card_holder_phone"}, None),
    ({"name": "card-holder", "type": "tel"}, None), ({"label": "Card holder date of birth"}, None),
    ({"label": "Card holder ID number"}, None), ({"label": "Card holder passport"}, None),
    ({"label": "Gift card holder"}, None), ({"label": "Loyalty card holder name"}, None),
    ({"name": "card_holder_first_name"}, None), ({"name": "card_holder_last_name"}, None),
    ({"name": "cardHolderFirstName"}, None), ({"label": "Card holder login"}, None),
    ({"name": "card-holder", "autocomplete": "username"}, None), ({"name": "card-holder", "label": "Merchant name"}, None),
    ({"name": "card-holder", "label": "Company"}, None), ({"name": "cardholderEmail"}, None),
    ({"name": "card", "label": "Holder"}, None),  # the holder words only across name and label
    # ... and the holder itself still classifies.
    ({"name": " card-holder", "label": "  Max Mustermann", "autocomplete": "off"}, "cc-name"),
    ({"name": "card-holder", "autocomplete": "cc-name"}, "cc-name"), ({"name": "card-holder", "autocomplete": "name"}, "cc-name"),
    ({"label": "Name of the card holder"}, "cc-name"), ({"name": "card-holder-name"}, "cc-name"),
])
def test_a_holder_cue_beside_another_purpose_keeps_that_purpose(control, token):
    assert _token(**control) == token


@pytest.mark.parametrize("control, token", [
    # camelCase names (Adyen's encrypted field ids among them)
    ({"name": "cardHolderName"}, "cc-name"), ({"name": "cardholderName"}, "cc-name"),
    ({"name": "creditCardNumber"}, "cc-number"), ({"name": " encryptedCardNumber"}, "cc-number"),
    ({"name": " encryptedSecurityCode"}, "cc-csc"), ({"name": " encryptedExpiryDate"}, "cc-exp"),
    ({"name": "cardCvv"}, "cc-csc"), ({"name": "cardExpiry"}, "cc-exp"), ({"name": "ccExp"}, "cc-exp"),
    ({"name": "cardExpiryMonth"}, "cc-exp-month"), ({"name": "cardExpireMonth", "type": "select"}, "cc-exp-month"),
    ({"name": "cardExpireYear", "type": "select"}, "cc-exp-year"),
    # Round 5: the expiry reach beyond the base wording needs a card word or a card field's own name.
    ({"name": "expireMonth", "type": "select"}, None), ({"name": "expireYear", "type": "select"}, None),
    # separated and numbered names, German and Romance labels, placeholders
    ({"name": "cc-number"}, "cc-number"), ({"name": "cc_number"}, "cc-number"), ({"name": "cvv2"}, "cc-csc"),
    ({"label": "CVV2"}, "cc-csc"), ({"name": "cvc2"}, "cc-csc"), ({"label": "Card verification code"}, "cc-csc"),
    ({"label": "Kreditkartennummer"}, "cc-number"), ({"label": "Prüfnr."}, "cc-csc"),
    ({"label": "Kartenprüfziffer"}, "cc-csc"), ({"label": "Kartensicherheitscode"}, "cc-csc"),
    ({"label": "Karte Gültigkeitsdatum"}, "cc-exp"), ({"label": "Karte MM / JJ"}, "cc-exp"),
    ({"label": "Card MM/AA"}, "cc-exp"), ({"label": "Card MM/YYYY"}, "cc-exp"), ({"label": "Card expires"}, "cc-exp"),
    ({"label": "Credit card valid until"}, "cc-exp"), ({"label": "Carte Expire le"}, "cc-exp"),
    ({"name": "ccValidUntil"}, "cc-exp"), ({"name": "cardValidUntil"}, "cc-exp"),
    # ... the same wording with no card word in the field's own text names a document, a pass, a subscription.
    ({"label": "Gültigkeitsdatum"}, None), ({"label": "MM / JJ"}, None), ({"label": "MM/AA"}, None),
    ({"label": "MM/YYYY"}, None), ({"label": "Expires"}, None), ({"label": "Valid until"}, None),
    ({"label": "Expire le"}, None), ({"name": "validUntil"}, None),
    ({"label": "Name on the card"}, "cc-name"), ({"label": "Name as it appears on card"}, "cc-name"),
    ({"label": "Nom sur la carte"}, "cc-name"), ({"label": "Titolare della carta"}, "cc-name"),
    ({"label": "Naam op kaart"}, "cc-name"), ({"label": "Nombre en la tarjeta"}, "cc-name"),
    # Non-card camelCase names stay out; an issue date stays out however it is spelled.
    ({"name": "promoCode"}, None), ({"name": "giftCardPin"}, None), ({"name": "companyName"}, None),
    ({"name": "firstName"}, None), ({"name": "panelId"}, None), ({"name": "customerEmail"}, None),
    ({"name": "issueDate"}, None), ({"name": "validFromDate"}, None), ({"name": "startMonth"}, None),
    ({"name": "userName"}, None), ({"name": "phoneNumber"}, None), ({"label": "Valid from MM/YY"}, None),
    ({"label": "SMS verification code"}, None),
    # camelCase splitting is for the card rows only: an address-like camelCase name is not read as one
    ({"name": "unitPrice"}, None),
    # A bare month/year select (a date of birth whose legend is outside the label reads the same) stays out.
    ({"name": "month", "type": "select"}, None), ({"name": "year", "type": "select"}, None),
    ({"label": "Monat", "type": "select"}, None),
])
def test_card_sibling_names_and_labels_classify(control, token):
    assert _token(**control) == token


@pytest.mark.parametrize("control, token", [
    ({"label": "Exp. MM/YY"}, "cc-exp"), ({"label": "EXP MM/YY"}, "cc-exp"), ({"label": "Exp (MM/YY)"}, "cc-exp"),
    ({"label": "Exp MM / YYYY"}, "cc-exp"), ({"name": "exp_mm_yy"}, "cc-exp"), ({"label": "Expiry (MM/YY)"}, "cc-exp"),
    # explicit month and year fields keep their tokens
    ({"name": "exp_mm"}, "cc-exp-month"), ({"label": "Exp MM"}, "cc-exp-month"), ({"label": "Expiry month"}, "cc-exp-month"),
    ({"name": "exp_yy"}, "cc-exp-year"), ({"label": "Expiry year (YYYY)"}, "cc-exp-year"),
    ({"name": "expiry-month"}, "cc-exp-month"), ({"name": "expiry-year", "maxLength": 2}, "cc-exp-year"),
])
def test_a_combined_expiry_with_a_month_shape_is_the_combined_expiry(control, token):
    assert _token(**control) == token


@pytest.mark.parametrize("name, label", [
    ("giftcard", "Gift card number"), ("giftcard", "Gift card code"), ("gc", "Gift card PIN"),
    ("payback", "PAYBACK Kartennummer"), ("mm", "Miles & More Kartennummer"), ("loyalty", "Loyalty card number"),
    ("membership", "Membership card number"), ("kundenkarte", "Kundenkartennummer"), ("voucher", "Gutscheincode"),
    ("promo", "Promo card code"), ("ticket", "Ticket expiry date"), ("gift", "Geschenkkarte: Kartennummer"),
    ("cc", "Customer card number"), ("giftCardNumber", ""),
])
def test_gift_loyalty_and_ticket_cards_are_never_card_fields(name, label):
    assert _token(name=name, label=label) is None


def test_a_decoy_word_elsewhere_in_a_real_card_field_name_does_not_hide_it():
    # A ticket merchant's own field name around a bank card field: the cue must qualify the card itself.
    assert _token(name="ticketPaymentCardNumber") == "cc-number"
    assert _token(name="ticket-checkout-cvc") == "cc-csc"


@pytest.mark.parametrize("control, token", [
    # Supplemental text (aria-labelledby, title) never promotes a field whose own evidence names another purpose:
    # autocomplete, input type, or its own name/label.
    ({"name": "phone ", "label": "Phone", "type": "tel", "autocomplete": "tel", "hint": "Card number"}, None),
    ({"name": "phone ", "label": "Phone", "hint": "Card number"}, None),
    ({"name": "f1 ", "type": "tel", "hint": "Card number"}, None), ({"name": "f1 ", "autocomplete": "tel",
                                                                     "hint": "Card number"}, None),
    ({"name": "f1 ", "type": "number", "hint": "CVC"}, None), ({"name": "qty ", "hint": "Card number"}, None),
    ({"name": "name ", "label": "Full name", "hint": "Card number"}, None),
    ({"name": "promo ", "hint": "Security code"}, None), ({"name": "dob ", "hint": "Expiry date (MM/YY)"}, None),
    ({"name": "f1 ", "label": "Gift card", "hint": "Card number"}, None),  # a primary decoy is never overridden
    ({"name": "cardholderEmail ", "hint": "Name on card"}, None),
    # The hint never overrides the field's own classification either way.
    ({"name": "ccnum ", "label": "Card number", "hint": "Phone"}, "cc-number"),
    ({"name": "zip ", "label": "ZIP code", "hint": "Card number"}, "postal-code"),
    # ... and it still names a field whose own text is silent, as it was added for.
    ({"name": " ", "label": "  ", "hint": "Card number"}, "cc-number"),
    ({"name": "f-3 field-3", "label": "  ", "hint": "Expiry date"}, "cc-exp"),
    ({"name": "h h", "label": "  ••••", "hint": "Name on card"}, "cc-name"),
    # Round 4: the own words must be generic ids; a placeholder name or a bare "name" blocks the hint.
    ({"name": "h h", "label": "  Max Mustermann", "hint": "Name on card"}, None),
    ({"name": "name ", "label": "  ", "hint": "Name on card"}, None),
    ({"name": "c ", "type": "password", "hint": "CVC"}, "cc-csc"),
    ({"name": "m ", "type": "select", "hint": "Expiry month"}, "cc-exp-month"),
    ({"name": " ", "hint": "Gift card number"}, None),  # the hint's own decoy rule still applies
])
def test_supplemental_text_only_names_a_field_whose_own_text_is_silent(control, token):
    assert _token(**control) == token


@pytest.mark.parametrize("control, token", [
    # An identity document, passport, visa or birth date is never a card field.
    ({"name": "passport_validity ", "label": "Passport valid until"}, None),
    ({"name": "ausweis ", "label": "Ausweis gültig bis"}, None), ({"label": "Personalausweis Ablaufdatum"}, None),
    ({"label": "Reisepass gültig bis"}, None), ({"label": "Document expiry date"}, None),
    ({"label": "ID card valid until"}, None), ({"label": "ID card number"}, None),
    ({"label": "Driver licence expiry"}, None), ({"label": "Travel visa expiry date"}, None),
    ({"label": "Visa expiry (passport)"}, None), ({"label": "Date of birth (MM/YY)"}, None),
    ({"label": "Geburtsdatum MM/JJ"}, None),
    # The card's own expiry keeps its token, a card brand beside "Visa" included.
    ({"label": "Card valid until"}, "cc-exp"), ({"label": "Visa card expiry"}, "cc-exp"),
    ({"label": "Visa / Mastercard expiry date"}, "cc-exp"), ({"label": "Card number (Visa, Mastercard)"}, "cc-number"),
    # "Visa" alone is the card brand where the base wording already reads it ("Visa expiry date" as on base). The wider
    # reach (round 5) needs a card word: "Visa" is no card evidence for "valid until", camelCase names, "expires".
    ({"label": "Visa expiry date"}, "cc-exp"), ({"label": "Expiry date (Visa)"}, "cc-exp"), ({"label": "Visa CVC"}, "cc-csc"),
    ({"name": "visaValidUntil"}, None), ({"name": "visaExpiry"}, None), ({"name": "visa-valid-until"}, None),
    ({"label": "Visa valid until"}, None), ({"name": "visaExpMonth"}, None), ({"label": "ESTA valid until"}, None),
    ({"label": "Licence valid until"}, None), ({"label": "Subscription valid until"}, None),
    ({"label": "Offer valid until"}, None), ({"label": "Visa card valid until"}, "cc-exp"),
])
def test_document_validity_is_never_the_card_expiry(control, token):
    assert _token(**control) == token


@pytest.mark.parametrize("control, token", [
    # A full-name instruction is no name component; a DOM id suffix is no identification cue.
    ({"name": "cardholder ", "label": "Name on card (first and last name)"}, "cc-name"),
    ({"name": "cardholder ", "label": "Karteninhaber Vorname Nachname"}, "cc-name"),
    ({"name": "cardholder ", "label": "Karteninhaber Vor- und Nachname"}, "cc-name"),
    ({"name": "cardholder ", "label": "Name on card (first name and last name)"}, "cc-name"),
    ({"name": "holder ", "label": "Titulaire de la carte prénom et nom"}, "cc-name"),
    ({"name": "cardholder cardholder-id", "label": "Name on card"}, "cc-name"),
    ({"name": "cardholder cardholder_id", "label": "Name on card"}, "cc-name"), ({"name": "cardholderId"}, "cc-name"),
    # a tel input whose text names the holder explicitly (in its label, or with a name word)
    ({"name": "h ", "label": "Name on card", "type": "tel"}, "cc-name"), ({"name": "cardholderName", "type": "tel"}, "cc-name"),
    ({"name": "h ", "label": "Karteninhaber", "type": "tel"}, "cc-name"),
    ({"name": "h ", "label": "Titolare della carta", "type": "tel"}, "cc-name"),
    # "card" is a word in the decoy rule, never the start of "cardholder"
    ({"name": "customerCardholder ", "label": "Name on card"}, "cc-name"), ({"name": "customer_cardholder_name"}, "cc-name"),
    # ... while a name component, an identification cue and a bare tel holder stay out
    ({"name": "cardholder ", "label": "First name"}, None), ({"name": "cardholder ", "label": "Nachname"}, None),
    ({"name": "cardholderFirstName", "label": "First and last name"}, None),
    ({"label": "Card holder ID"}, None), ({"label": "Card holder ID number"}, None),
    ({"name": "cardholder ", "label": "Ausweisnummer"}, None), ({"name": "cardholderDni"}, None),
    ({"name": "card-holder", "type": "tel"}, None), ({"name": "cardholder", "type": "tel"}, None),
    ({"label": "Card holder phone", "type": "tel"}, None), ({"name": "h ", "label": "Name on card", "type": "number"}, None),
    ({"name": "h ", "label": "Telefon des Karteninhabers", "type": "tel"}, None),
    ({"name": "h ", "label": "Mobilnummer Karteninhaber", "type": "tel"}, None),
    ({"label": "Adresse des Karteninhabers"}, None), ({"label": "Karteninhaber PLZ"}, None),
    # ... and the German compounds stay decoys
    ({"name": "k ", "label": "Kundenkartennummer"}, None), ({"label": "Geschenkkarte"}, None),
    ({"label": "Gutscheinkarte Nummer"}, None), ({"label": "PAYBACK Karte"}, None),
    ({"name": "cc ", "label": "Customer card number"}, None), ({"label": "Customer card holder"}, None),
])
def test_full_name_instructions_and_dom_ids_keep_the_holder(control, token):
    assert _token(**control) == token


def test_a_holder_cue_on_a_zip_field_never_plans_the_holder_name_there():
    from tools import browser_vault_payment as bvp

    rows = [{"index": n, "autocomplete": "", "name": name, "label": label, "type": "text", "formIndex": 0,
             "maxLength": None}
            for n, (name, label) in enumerate([("ccnum ", "Card number"), ("expiry ", "Expiry date"),
                                               ("cvc ", "CVC"), ("zip ", "Card holder ZIP code")])]
    controls = sp._card_controls(sp._bounded_description(rows))
    assert [c.token for c in controls] == ["cc-number", "cc-exp", "cc-csc"]  # the ZIP is no card field
    docs = _card_documents([("f", "s", "o", MERCHANT, controls)], MERCHANT)
    fills = bvp._document_fills(_inspection(*docs), CARD)
    assert sorted(f["token"] for f in fills) == ["cc-csc", "cc-exp", "cc-number"]
    assert all(f["index"] != 3 for f in fills)

    # The merchant's own "Name on card" beside billing "Card holder address/city" fields fills as on base: four
    # fields, the holder into "Name on card" only.
    rows += [{**rows[0], "index": 4, "name": "name ", "label": "Name on card"},
             {**rows[0], "index": 5, "name": "addr ", "label": "Card holder address"},
             {**rows[0], "index": 6, "name": "city ", "label": "Card holder city"}]
    controls = sp._card_controls(sp._bounded_description(rows))
    docs = _card_documents([("f", "s", "o", MERCHANT, controls)], MERCHANT)
    fills = bvp._document_fills(_inspection(*docs), CARD)
    assert [(f["index"], f["value"]) for f in fills if f["token"] == "cc-name"] == [(4, "Ada Lovelace")]
    assert len(fills) == 4


def test_a_merchant_holder_beside_unzer_holder_is_refused_for_the_duplicate_under_the_cap():
    # Four documents (Unzer UI v1's cap): merchant holder, Unzer number, CVC and holder frames, the expiry frame not
    # loaded. The duplicate holder is refused before the missing expiry is even looked at ...
    frames = {k: v for k, v in _unzer_frames().items() if k != "expiry"}
    duplicate = _unzer_candidates({**frames, "page": [_UNZER_HOLDER]}, {"page": MERCHANT})
    assert len(duplicate) == 4
    with pytest.raises(PaymentPolicyError, match="Incomplete or ambiguous card fields"):
        _card_documents(duplicate, MERCHANT)
    # ... while the same documents without the merchant's holder pass that check and stop at the expiry.
    with pytest.raises(PaymentPolicyError, match="Missing card expiry fields"):
        _card_documents(_unzer_candidates(frames), MERCHANT)


def _fill_unzer(monkeypatch, card):
    from tools import browser_vault_payment as bvp

    inspection = _inspection(*_card_documents(_unzer_candidates(_unzer_frames()), MERCHANT))
    committed = []

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            return True

        def commit_payment(self, i, fills, strict=False):
            committed.extend(fills)
            return asyncio.run(_PerFrameSupervisor([{"success": True, "filled_fields": 1}] * 4)._commit_payment(i, fills))

        def discard_payment(self, i):
            pass

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda task_id, surface=None: None)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *args, **k: "accept")
    from agent import vault_payment_policy as policy

    unzer = next(p for p in policy.OPT_IN_PROVIDERS if p.name == "unzer-ui-v1")
    monkeypatch.setattr(policy, "enabled_providers", lambda: policy.DEFAULT_PROVIDERS + (unzer,))
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None,
                           payment_scope="confirmed_page", payment_frame_origins=())
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: dict(card), get_meta=lambda handle: meta)
    return json.loads(bvp.fill_payment(backend, meta, "task")), committed


def test_the_unzer_fill_reports_the_holder_it_wrote(monkeypatch):
    out, committed = _fill_unzer(monkeypatch, CARD)
    assert out["success"] and out["retention_verified"] and out["filled_fields"] == 4, out
    assert out["fields"] == ["cc-csc", "cc-exp", "cc-name", "cc-number"] and out["skipped_optional"] == []
    assert [(f["document"], f["token"]) for f in committed if f["token"] == "cc-name"] == [(3, "cc-name")]


def test_an_unzer_holder_the_saved_card_has_no_name_for_is_reported_skipped(monkeypatch):
    """The holder input was found but the card has no holder name: the other fields are filled and the result says
    the holder was not (the provider may still require it), instead of omitting it as an unrecognised frame did."""
    card = {k: v for k, v in CARD.items() if k != "cardholder_name"}
    out, committed = _fill_unzer(monkeypatch, card)
    assert out["success"] and out["filled_fields"] == 3, out
    assert out["fields"] == ["cc-csc", "cc-exp", "cc-number"] and out["skipped_optional"] == ["cc-name"]
    assert sorted(f["document"] for f in committed) == [0, 1, 2]  # the holder document is not written


def test_expiry_fits_the_field_length():
    from agent.vault_login_classifier import select_checkout_fills
    from agent.vault_store import PAYMENT_FIELDS

    two = [classify_checkout_control(LoginControl.from_dict(r)) for r in (
        {"index": 0, "autocomplete": "cc-exp-month"}, {"index": 1, "autocomplete": "cc-exp-year", "maxLength": 2})]
    four = [two[0], classify_checkout_control(LoginControl.from_dict({"index": 1, "autocomplete": "cc-exp-year"}))]
    card = {**CARD, "exp_month": "7"}
    assert [f["value"] for f in select_checkout_fills(two, card, PAYMENT_FIELDS)] == ["07", "35"]
    assert [f["value"] for f in select_checkout_fills(four, card, PAYMENT_FIELDS)] == ["07", "2035"]


_PAYPAL = "https://www.paypal.com"


def _four_hosted_documents():
    return _inspection(*(PaymentDocument(f"f{n}", f"s{n}", f"o{n}", f"{_PAYPAL}#{n}", _controls(t))
                         for n, t in enumerate(("cc-name", "cc-exp", "cc-csc", "cc-number"))))


def _four_fills():
    return [{"index": 0, "token": t, "value": v, "document": n} for n, (t, v) in enumerate(
        (("cc-name", "Ada Lovelace"), ("cc-exp", "07/35"), ("cc-csc", "837"), ("cc-number", "4242424242424242")))]


def test_success_needs_every_document_to_hold_after_settling():
    """The reported shape: four hosted documents each pass their own check, then the number and CVC
    are empty. Before the settled check this returned success with filled_fields 4."""
    sup = _FakeSupervisor([{"success": True, "filled_fields": 1}] * 4, retained={"cc-number": "empty", "cc-csc": "empty"})
    out = asyncio.run(sup._commit_payment(_four_hosted_documents(), _four_fills()))
    assert out == {"success": False, "error_type": "payment_fields_not_retained", "filled_fields": 2,
                   "partially_filled": True,
                   "field_retention": {"cc-csc": "empty", "cc-exp": "held", "cc-name": "held", "cc-number": "empty"},
                   "field_lengths": {"cc-csc": 0, "cc-number": 0}}
    assert sup.released and len(sup.writes) == 4  # written once each, never retried


def test_an_unreadable_document_after_settling_counts_as_replaced():
    sup = _FakeSupervisor([{"success": True, "filled_fields": 1}] * 4, retained={f"{_PAYPAL}#3": "gone"})
    out = asyncio.run(sup._commit_payment(_four_hosted_documents(), _four_fills()))
    assert out["error_type"] == "payment_fields_not_retained" and out["field_retention"]["cc-number"] == "replaced"


def test_retention_result_keeps_only_tokens_states_and_bounded_lengths():
    fills = [{"token": "cc-number", "value": "4242424242424242"}, {"token": "cc-csc", "value": "837"}]
    answer = [{"token": "cc-number", "state": "changed", "length": 19, "value": "4242424242424242"},
              {"token": "cc-csc", "state": "4242", "length": "837"}]
    out = sp._retention_result([(fills, answer)])
    assert out["field_retention"] == {"cc-csc": "replaced", "cc-number": "changed"}
    assert out["field_lengths"] == {"cc-csc": 0, "cc-number": 19}
    assert "4242" not in json.dumps(out) and "837" not in json.dumps(out)
    # Missing, extra or mismatched answers are never read as held.
    for bad in (None, [], answer + [{"token": "cc-name", "state": "held"}], "junk",
                [{"token": "cc-csc", "state": "held"}, {"token": "cc-number", "state": "held"}]):
        assert sp._retention_result([(fills, bad)])["field_retention"] == {"cc-csc": "replaced", "cc-number": "replaced"}


def _fill_with_consent(monkeypatch, verdict, reason, on_consent=None, expires_in=None, on_current=None):
    import time as _time

    from tools import browser_vault_payment as bvp

    inspection = _four_hosted_documents()
    if expires_in is not None:
        inspection.expires_at = _time.monotonic() + expires_in
    calls = []

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            calls.append("payment_is_current")
            if on_current:
                on_current(i)
            return True

        def commit_payment(self, i, fills, strict=False):
            calls.append("commit_payment")
            return {"success": True}

        def discard_payment(self, i):
            calls.append("discard_payment")

    def consent(*args, outcome=None, **kwargs):
        if outcome is not None and reason:
            outcome["reason"] = reason
        if on_consent:
            on_consent(inspection, kwargs)
        return verdict

    def resolve(handle):
        calls.append("resolve_secret")
        return dict(CARD)

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session",
                        lambda task_id, surface=None: calls.append("mark_payment_session"))
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", consent)
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)
    raw = bvp.fill_payment(SimpleNamespace(name="local", resolve_secret=resolve, get_meta=lambda handle: meta),
                           meta, "task")
    return json.loads(raw), calls


@pytest.mark.parametrize("verdict, reason, expected, error_type", [
    ("decline", "refused", "refused", "payment_declined"),
    ("cancel", "timeout", "timeout", "payment_consent_missing"),
    ("decline", "unavailable", "unavailable", "payment_consent_missing"),
    ("decline", None, "refused", "payment_declined"), ("cancel", None, "timeout", "payment_consent_missing"),
])
def test_consent_not_given_stops_before_any_secret_or_write(monkeypatch, verdict, reason, expected, error_type):
    out, calls = _fill_with_consent(monkeypatch, verdict, reason)
    # Only an explicit refusal is payment_declined (never retried); a missing answer is its own type.
    assert out["success"] is False and out["error_type"] == error_type
    assert out["consent_outcome"] == expected and out["issuer_response"] is False
    assert "bank or issuer decline" in out["error"]
    if error_type == "payment_consent_missing":
        assert "new consent prompt" in out["error"] and "ask the user once" in out["error"]
    assert calls == ["discard_payment"]  # no freshness pass, no secret, no capture mark, no write


def test_consent_is_bounded_by_the_inspection_window_and_a_late_accept_writes_nothing(monkeypatch):
    import time as _time

    asked = {}

    def late(inspection, kwargs):
        asked["timeout"] = kwargs.get("timeout_seconds")
        inspection.expires_at = _time.monotonic() - 1  # the accept lands after the window closed

    out, calls = _fill_with_consent(monkeypatch, "accept", "accepted", on_consent=late, expires_in=40)
    assert 30 <= asked["timeout"] <= 40  # the prompt closes with the inspection window
    assert out["error_type"] == "payment_consent_expired" and "5-minute inspection window" in out["error"]
    assert calls == ["discard_payment"]  # no freshness pass, no secret, no write


def test_a_window_that_closes_during_the_freshness_pass_resolves_no_secret(monkeypatch):
    """119 s at the post-consent check, 121 s once the slow freshness pass returns: no secret, no write."""
    import time as _time

    def lapse(i):
        i.expires_at = _time.monotonic() - 1

    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    out, calls = _fill_with_consent(monkeypatch, "accept", "accepted", on_current=lapse, expires_in=60)
    assert out["error_type"] == "payment_consent_expired"
    assert "resolve_secret" not in calls and "commit_payment" not in calls


@pytest.mark.parametrize("lapse_at", ["freshness", "second_document"])
def test_commit_never_writes_after_the_window_closes(lapse_at):
    import time as _time

    i = _split_inspection()
    i.expires_at = _time.monotonic() + 60

    class Supervisor(_FakeSupervisor):
        async def _payment_is_current(self, inspection):
            if lapse_at == "freshness":
                inspection.expires_at = 0
            return True

        async def _commit_payment_document(self, d, fills, deadline_ms=None, strict=False, paced=None, **_kw):
            out = await super()._commit_payment_document(d, fills, deadline_ms)
            i.expires_at = 0  # the first document's write ends after the window
            return out

    sup = Supervisor([{"success": True, "filled_fields": 1}] * 2)
    out = asyncio.run(sup._commit_payment(i, [{"index": 0, "token": "cc-number", "value": "4242", "document": 0},
                                              {"index": 0, "token": "cc-csc", "value": "837", "document": 1}]))
    if lapse_at == "freshness":
        assert out == {"success": False, "error_type": "payment_consent_expired"} and not sup.writes
    else:  # a truthful partial stop, never success
        assert out == {"success": False, "error_type": "payment_consent_expired", "filled_fields": 1,
                       "partially_filled": True} and len(sup.writes) == 1


class _OwnerSupervisor(sp.PaymentSupervisionMixin):
    """Real commit/retention/visibility code over a fake CDP: ``shown(pass_no, frame)`` answers the owner check."""

    def __init__(self, shown):
        self.shown, self.passes, self.calls = shown, 0, []

    async def _payment_is_current(self, i):
        return True

    async def _payment_scope_current(self, i, documents):
        return True

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        self.calls.append(method)
        fn = (params or {}).get("functionDeclaration", "")
        if fn == sp._OWNERS_SHOWN:
            self.passes += 1
            frames = [o["objectId"] for o in params["arguments"]]
            return {"result": {"result": {"value": [self.shown(self.passes, f) for f in frames]}}}
        if "commit(" in fn:
            return {"result": {"result": {"value": {"success": True, "filled_fields": 1}}}}
        if "retained()" in fn:
            return {"result": {"result": {"value": [{"token": "cc-number", "state": "held", "length": 4}]}}}
        return {"result": {}}


def _owned_inspection(tree, docs):
    import time as _time

    by_id = {f[0]: f for f in tree}
    owners = {}
    for d in docs:
        fid = d.frame_id
        while by_id[fid][4] and fid not in [x for _s, items in owners.values() for x, _o in items]:
            owners.setdefault(by_id[fid][4], ("s-" + by_id[fid][4], []))[1].append((fid, fid))
            fid = by_id[fid][4]
    return PaymentInspection("op", "page", "target", MERCHANT, tuple(docs), tree, _time.monotonic() + 60,
                             frame_owners=owners)


@pytest.mark.parametrize("hidden", ["ancestor", "during_collection", "transparent_owner_answer"])
def test_retention_needs_every_frame_shown_before_and_after_the_reads(hidden):
    """A guest card form inside /smart/buttons: hiding the buttons frame hides the card frame too, and a
    frame hidden while the answers are collected is not verified either."""
    tree = (("top", "", "", MERCHANT, ""), ("btn", "", "", _PAYPAL, "top"), ("card", "", "", _PAYPAL, "btn"))
    doc = PaymentDocument("card", "sc", "oc", _PAYPAL, _controls("cc-number"))
    shown = {"ancestor": lambda n, f: f != "btn", "during_collection": lambda n, f: n <= 2,
             "transparent_owner_answer": lambda n, f: "yes"}[hidden]  # only a literal true counts
    out = asyncio.run(_OwnerSupervisor(shown)._commit_payment(
        _owned_inspection(tree, [doc]), [{"index": 0, "token": "cc-number", "value": "4242", "document": 0}]))
    assert out["error_type"] == "payment_fields_not_retained" and out["field_retention"] == {"cc-number": "replaced"}


def test_four_hosted_frames_cost_one_visibility_call_per_parent_per_pass():
    """The PayPal layout (four card frames in the merchant page) must stay inside commit's 20 s budget:
    two batched owner checks in total, not several CDP calls per frame per pass."""
    tree = (("top", "", "", MERCHANT, ""),) + tuple((f"f{n}", "", "", _PAYPAL, "top") for n in range(4))
    docs = [PaymentDocument(f"f{n}", f"s{n}", f"o{n}", _PAYPAL, _controls(t))
            for n, t in enumerate(("cc-name", "cc-exp", "cc-csc", "cc-number"))]
    sup = _OwnerSupervisor(lambda n, f: True)
    fills = [{"index": 0, "token": t, "value": v, "document": n} for n, (t, v) in enumerate(
        (("cc-name", "Ada"), ("cc-exp", "07/35"), ("cc-csc", "837"), ("cc-number", "4242")))]
    i = _owned_inspection(tree, docs)
    # Each fake document answers for cc-number; only the call budget is under test here.
    asyncio.run(sup._commit_payment(i, fills))
    assert sup.passes == 2
    assert sup.calls.count("Runtime.callFunctionOn") == 2 + 4 + 4  # owner checks + commits + retention reads


@pytest.mark.parametrize("consent_s, expected_ms", [(30, 20_000), (5, 5_000)])
def test_renderer_deadline_is_the_earlier_of_consent_and_commit_budget(consent_s, expected_ms):
    """A write dispatched just inside the 20 s commit budget must not land after it once the host has
    reported uncertainty: the renderer gets min(consent, budget), counted from its closure anchor."""
    import time as _time

    sup, i = _FakeSupervisor([{"success": True, "filled_fields": 1}] * 2), _split_inspection()
    now = _time.monotonic()
    i.expires_at = now + consent_s
    i.documents[0].clock_anchor = now
    asyncio.run(sup._commit_payment(i, [{"index": 0, "token": "cc-csc", "value": "837", "document": 0},
                                        {"index": 0, "token": "cc-number", "value": "4242", "document": 1}],
                                    until=now + 20))
    first, unanchored = sup.deadlines
    assert abs(first - expected_ms) < 1 and unanchored == 0.0  # no anchor: nothing may be written


_COMMIT_HARNESS = r"""
let mono = 1000, wallNow = CASE.wallStart, writes = 0;
const performance = {now: () => mono};
Date.now = () => wallNow;
class HTMLInputElement { get value() { return this.held || ''; } set value(v) { writes += 1; this.held = v; } }
class HTMLSelectElement { get value() { return ''; } set value(v) {} }
class Event {} class InputEvent extends Event {} class FocusEvent extends Event {}
const document = {forms: []};
const el = Object.assign(new HTMLInputElement(), {tagName: 'INPUT', autocomplete: 'cc-number', name: 'n', id: 'n',
  labels: null, placeholder: '', type: 'text', form: null, maxLength: 0, disabled: false, readOnly: false,
  isConnected: true, tabIndex: 0, shadowRoot: null, ownerDocument: document, style: {setProperty() {}},
  getAttribute: () => null, hasAttribute: () => false, getBoundingClientRect: () => ({width: 100, height: 20}),
  getClientRects: () => [1], dispatchEvent: () => true, blur() {}, focus() { mono += CASE.focus; }});
document.querySelectorAll = () => [el];
const location = {href: 'https://www.paypal.com/card'};
const getComputedStyle = () => ({visibility: 'visible', opacity: '1', pointerEvents: 'auto'});
const api = INSPECT;
mono += CASE.elapsed;
wallNow += CASE.wallElapsed;
const result = api.commit([{index: 0, token: 'cc-number', value: '4242'}], CASE.deadline);
process.stdout.write(JSON.stringify({result, writes}));
"""



_DESCRIBE_HARNESS = r"""
let mono = 1000;
const performance = {now: () => mono};
class HTMLInputElement { get value() { return this.held || ''; } set value(v) { this.held = v; } }
class HTMLSelectElement { get value() { return ''; } set value(v) {} }
class Event {} class InputEvent extends Event {} class FocusEvent extends Event {}
const texts = {lbl: 'Name on card', hint: 'x'.repeat(500), outer: 'outside the root'};
const root = {getElementById: id => (id in texts && id !== 'outer' ? {textContent: texts[id]} : null)};
const document = {forms: [], getElementById: id => (id === 'outer' ? {textContent: texts.outer} : null)};
const attrs = CASE.attrs;
const el = Object.assign(new HTMLInputElement(), {tagName: 'INPUT', autocomplete: 'off', name: 'h', id: 'h',
  labels: null, placeholder: 'Max Mustermann', type: 'text', form: null, maxLength: 0, disabled: false, readOnly: false,
  isConnected: true, tabIndex: 0, shadowRoot: null, ownerDocument: document, style: {setProperty() {}}, held: 'secret',
  getAttribute: n => (n in attrs ? attrs[n] : null), hasAttribute: () => false,
  getBoundingClientRect: () => ({width: 100, height: 20}), getClientRects: () => [1], dispatchEvent: () => true,
  blur() {}, focus() {}, ...(CASE.root ? {getRootNode: () => root} : {})});
document.querySelectorAll = () => [el];
const location = {href: 'https://payment.example/holder'};
const getComputedStyle = () => ({visibility: 'visible', opacity: '1', pointerEvents: 'auto'});
const api = INSPECT;
process.stdout.write(JSON.stringify(api.describe()));
"""


@pytest.mark.parametrize("case, hint", [
    # aria-labelledby (in the field's own root, then the document) and title make the hint, each capped at 200; the
    # label stays the field's own text (labels, aria-label, placeholder)
    ({"attrs": {"aria-labelledby": "lbl outer", "title": "Karteninhaber"}, "root": True},
     "Name on card outside the root Karteninhaber"),
    ({"attrs": {"aria-labelledby": "hint"}, "root": True}, "x" * 200 + " "),
    # no root to ask (an element without getRootNode), an unknown id, nothing set: no hint text
    ({"attrs": {"aria-labelledby": "lbl"}, "root": False}, " "),
    ({"attrs": {"aria-labelledby": "missing"}, "root": True}, " "),
    ({"attrs": {}, "root": True}, " "),
])
def test_the_card_descriptor_reads_aria_labelledby_and_title_but_never_the_value(case, hint):
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic inspection closure")
    values = {"CASE": json.dumps(case), "INSPECT": sp._INSPECT}
    harness = re.sub(r"\bCASE\b|\bINSPECT\b", lambda m: values[m.group(0)], _DESCRIBE_HARNESS)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    (row,) = json.loads(run.stdout)
    assert row["label"] == "  Max Mustermann" and row["hint"] == hint and "secret" not in run.stdout
    if case["attrs"].get("aria-labelledby") == "lbl outer":
        assert sp._bounded_description([row]) == [row]  # the discovery shape check accepts the hint key
        # Round 4: the placeholder "Max Mustermann" is own text that is no generic id, so the hint names nothing.
        # Round 5: the hint is also several holder phrases and "outside the root", not one role plus neutral words,
        # so it names nothing either; with only generic own words ("h h") a hint of the one role names the holder.
        assert classify_checkout_control(LoginControl.from_dict(row)) is None
        assert classify_checkout_control(LoginControl.from_dict({**row, "label": "  "})) is None
        assert classify_checkout_control(LoginControl.from_dict({**row, "label": "  ", "hint": "Name on card"})).token == "cc-name"


# The page's fields go through the production chain: the closure's describe(), discovery's shape check, the
# classifier, the document check, planning, and the same closure's commit() with that plan (its writes recorded per
# element id).
_PIPELINE_HARNESS = r"""
const writes = [];
const performance = {now: () => 1000};
class HTMLInputElement { get value() { return this.held || ''; } set value(v) { writes.push([this.id, v]); this.held = v; } }
class HTMLSelectElement { get value() { return ''; } set value(v) {} }
class Event {} class InputEvent extends Event {} class FocusEvent extends Event {}
const document = {forms: [], getElementById: id => (id in CASE.texts ? {textContent: CASE.texts[id]} : null)};
const location = {href: 'https://shop.example/checkout'};
const els = CASE.fields.map(f => Object.assign(new HTMLInputElement(), {tagName: 'INPUT', autocomplete: f.ac || '',
  name: f.name, id: f.id, labels: f.label ? [{textContent: f.label}] : null, placeholder: f.placeholder || '', type: f.type || 'text',
  form: null, maxLength: 0, disabled: false, readOnly: false, isConnected: true, tabIndex: 0, shadowRoot: null,
  ownerDocument: document, style: {setProperty() {}}, hasAttribute: () => false, dispatchEvent: () => true, blur() {},
  focus() {}, getAttribute: n => ((f.attrs || {})[n] ?? null), getBoundingClientRect: () => ({width: 100, height: 20}),
  getClientRects: () => [1], getRootNode: () => document}));
document.querySelectorAll = () => els;
const getComputedStyle = () => ({visibility: 'visible', opacity: '1', pointerEvents: 'auto'});
const api = INSPECT;
process.stdout.write(JSON.stringify(CASE.plan ? {result: api.commit(CASE.plan, 60000), writes} : api.describe()));
"""


def _pipeline(fields: list, texts: dict | None = None) -> tuple:
    """``(planned fills or the refusal message, the commit's writes [[element id, value]] or None)``."""
    import re
    import shutil
    import subprocess

    from tools import browser_vault_payment as bvp

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic inspection closure")

    def run(case):
        values = {"CASE": json.dumps(case), "INSPECT": sp._INSPECT}
        harness = re.sub(r"\bCASE\b|\bINSPECT\b", lambda m: values[m.group(0)], _PIPELINE_HARNESS)
        out = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    case = {"fields": fields, "texts": texts or {}}
    controls = sp._card_controls(sp._bounded_description(run(case)))
    try:
        docs = _card_documents([("f", "s", "o", MERCHANT, controls)], MERCHANT)
    except PaymentPolicyError as exc:
        return str(exc), None
    fills = bvp._document_fills(_inspection(*docs), CARD)
    plan = [{"index": f["index"], "token": f["token"], "value": f["value"]} for f in fills]
    out = run({**case, "plan": plan})
    assert out["result"]["success"] is True, out["result"]
    return fills, out["writes"]


_NUMBER = {"name": "ccnum", "id": "ccnum", "label": "Card number"}
_EXPIRY = {"name": "exp", "id": "exp", "label": "Expiry date"}
_CVC = {"name": "cvc", "id": "cvc", "label": "CVC"}
_PHONE = {"name": "phone", "id": "phone", "label": "Phone", "type": "tel", "ac": "tel"}
_PAN = CARD["card_number"]


@pytest.mark.parametrize("phone_attrs, texts", [
    ({"title": "Card number"}, {}), ({"aria-labelledby": "pan-label"}, {"pan-label": "Card number"}),
])
def test_a_title_or_labelledby_card_number_on_a_phone_input_never_receives_the_card_number(phone_attrs, texts):
    # Codex r2 F1: the phone input beside expiry and CVC is refused as an incomplete card form; nothing is written.
    phone = {**_PHONE, "attrs": phone_attrs}
    assert _pipeline([phone, _EXPIRY, _CVC], texts) == ("Incomplete or ambiguous card fields", None)
    # Beside a real number field the card number goes there, never into the phone input.
    _fills, writes = _pipeline([phone, _NUMBER, _EXPIRY, _CVC], texts)
    assert ["ccnum", _PAN] in writes and all(element != "phone" for element, _value in writes)
    assert len(writes) == 3
    # The same title on an input with no text of its own still names the number (what the hint is for).
    bare = {"name": "", "id": "", "attrs": phone_attrs}
    _fills, writes = _pipeline([bare, _EXPIRY, _CVC], texts)
    assert ["", _PAN] in writes and len(writes) == 3


def test_a_passport_validity_field_never_receives_the_card_expiry():
    # Codex r2 F2: with number and CVC only, the passport field is no expiry: refused, nothing written ...
    passport = {"name": "passport_validity", "id": "pv", "label": "Passport valid until"}
    assert _pipeline([_NUMBER, _CVC, passport]) == ("Missing card expiry fields", None)
    # ... and beside the card's own expiry the form is not a duplicate: three writes, none into the passport field.
    fills, writes = _pipeline([_NUMBER, passport, _EXPIRY, _CVC])
    assert sorted(f["token"] for f in fills) == ["cc-csc", "cc-exp", "cc-number"]
    assert len(writes) == 3 and all(element != "pv" for element, _value in writes)


@pytest.mark.parametrize("holder", [
    {"name": "cardholder", "id": "h", "label": "Name on card (first and last name)"},
    {"name": "cardholder", "id": "h", "label": "Karteninhaber", "placeholder": "Vorname Nachname"},
    {"name": "cardholder", "id": "cardholder-id", "label": "Name on card"},
    {"name": "customerCardholder", "id": "h", "label": "Name on card"},
])
def test_codex_r2_holders_are_planned_and_written_again(holder):
    # Codex r2 F3/F4: these holders were dropped (no cc-name, three writes); the holder name goes into the holder.
    fields = [_NUMBER, _EXPIRY, _CVC, holder]
    fills, writes = _pipeline(fields)
    assert [f["value"] for f in fills if f["token"] == "cc-name"] == [CARD["cardholder_name"]]
    assert [holder["id"], CARD["cardholder_name"]] in writes and len(writes) == 4


@pytest.mark.parametrize("hint, accepted", [
    ("Card number", True), ("x" * 200, True), (" ", True), ("x" * 201, False), (7, False), (None, False),
])
def test_discovery_and_rebind_accept_only_a_bounded_string_hint(hint, accepted):
    from agent.vault_payment_policy import PaymentDestinationError
    from tools.browser_supervisor_payment_repin import _rebind_row

    row = {"index": 0, "autocomplete": "", "name": "f ", "label": " ", "type": "text", "formIndex": None,
           "maxLength": None, "hint": hint}
    if accepted:
        assert sp._bounded_description([row]) == [row]
    else:
        with pytest.raises(PaymentDestinationError):
            sp._bounded_description([row])
    # the reviewed closure's rebind has no text limit, only the type
    assert _rebind_row(row, -1) is (isinstance(hint, str))
    assert _rebind_row({k: v for k, v in row.items() if k != "hint"}, -1) is True


_T0 = 1_794_000_000_000  # the browser's wall clock at closure creation


@pytest.mark.parametrize("case, writes", [
    ({"elapsed": 500, "focus": 0, "deadline": 1000, "wallStart": _T0, "wallElapsed": 500}, 1),
    # past it on the monotonic clock, though the wall clock stepped back to the epoch
    ({"elapsed": 1500, "focus": 0, "deadline": 1000, "wallStart": _T0, "wallElapsed": -_T0}, 0),
    # page code on focus crossed it: no setter
    ({"elapsed": 900, "focus": 200, "deadline": 1000, "wallStart": _T0, "wallElapsed": 900}, 0),
    # no deadline is never "no limit"
    ({"elapsed": 0, "focus": 0, "deadline": None, "wallStart": _T0, "wallElapsed": 0}, 0),
    # a remote browser slept: its monotonic clock paused inside the window, wall time ran past it
    ({"elapsed": 500, "focus": 0, "deadline": 1000, "wallStart": _T0, "wallElapsed": 1100}, 0),
    # the same sleep with the browser's clock five seconds behind the host: an offset cancels out
    ({"elapsed": 500, "focus": 0, "deadline": 1000, "wallStart": _T0 - 5000, "wallElapsed": 1100}, 0),
])
def test_renderer_writes_only_while_both_its_monotonic_and_wall_clocks_are_inside(case, writes):
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic commit closure")
    values = {"CASE": json.dumps(case), "INSPECT": sp._INSPECT}
    harness = re.sub(r"\bCASE\b|\bINSPECT\b", lambda m: values[m.group(0)], _COMMIT_HARNESS)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["writes"] == writes
    assert out["result"]["success"] is bool(writes)
    if not writes:
        assert out["result"]["error_type"] == "payment_consent_expired"


_SETTER_HARNESS = r"""
let writes = [], els, cover = null, mono = 1000;
const performance = {now: () => mono};
Object.assign(globalThis, {innerWidth: 1000, innerHeight: 1000});
// The receiving frame's same-origin parent iframe and the top page (frameElement / parent / top).
const parentWin = {location: {href: 'https://shop.example/checkout'}};
const owner = {isConnected: true, op: '1', ownerDocument: {defaultView: {getComputedStyle: e => ({visibility: 'visible',
  opacity: e.op})}}, getBoundingClientRect: () => ({width: 300, height: 200}), getClientRects: () => [1]};
Object.assign(globalThis, {frameElement: owner, parent: parentWin, top: parentWin});
class HTMLInputElement { get value() { return this.held || ''; } set value(v) { writes.push(this.id); this.held = v; } }
class HTMLSelectElement { get value() { return ''; } set value(v) {} }
class Event {} class InputEvent extends Event {} class FocusEvent extends Event {}
const document = {forms: []};
const location = {href: 'https://psp.example/card'};
const mk = (id, token, top) => Object.assign(new HTMLInputElement(), {tagName: 'INPUT', autocomplete: token, name: id,
  id, labels: null, placeholder: '', type: 'text', form: null, maxLength: 0, disabled: false, readOnly: false,
  isConnected: true, tabIndex: 0, shadowRoot: null, ownerDocument: document, hidden: false, top,
  style: {setProperty() {}}, hasAttribute: () => false, dispatchEvent: () => true, blur() {},
  getAttribute(n) { return n === 'aria-label' ? (this.aria || null) : n === 'title' ? (this.tip || null) : null; },
  getBoundingClientRect() { if (this.slow) mono += 1000;
    return this.hidden ? {width: 0, height: 0, left: 0, top: 0} : {width: 100, height: 20, left: 0, top: this.top}; },
  getClientRects() { return this.hidden ? [] : [1]; }, getRootNode: () => document, contains(o) { return o === this; },
  focus() { if (this.onfocus) this.onfocus(); }});
els = [mk('c', 'cc-csc', 0), mk('n', 'cc-number', 40)];
document.querySelectorAll = () => els;
document.elementFromPoint = (x, y) => cover || els.find(e => !e.hidden && y >= e.top && y < e.top + 20) || null;
const getComputedStyle = e => ({visibility: e.hidden ? 'hidden' : 'visible', opacity: '1', pointerEvents: 'auto'});
const api = INSPECT;
const n = els[1];
n.onfocus = {none: () => {}, hide: () => { n.hidden = true; }, hide_other: () => { els[0].hidden = true; },
  relabel: () => { n.aria = 'Gift card'; }, retitle: () => { n.tip = 'Gift card'; }, swap: () => { els = [els[0], mk('n2', 'cc-number', 40)]; },
  navigate: () => { location.href = 'https://psp.example/elsewhere'; }, cover: () => { cover = {}; },
  parent_transparent: () => { owner.op = '0'; }, top_replaced: () => { parentWin.location.href += '?step=2'; },
  slow_checks: () => { n.slow = true; }}[CASE.focus];
let result = api.commit([{index: 0, token: 'cc-csc', value: '837'}, {index: 1, token: 'cc-number', value: '4242'}],
                        CASE.focus === 'slow_checks' ? 1500 : 60000, CASE.strict, CASE.paced === true,
                        ...(CASE.repin === true ? [true] : []));
const paused = [];  // [fields written so far, setter calls so far] at each pause
const repins = [];  // the check of each re-pin request; CASE.pins 'auto' answers by autocomplete, else null (stop)
while (result && (result.pending === true || result.repin === true) && paused.length + repins.length < 8) {
  if (result.repin === true) {
    repins.push(result.check);
    const seen = api.rebind();
    let pins = null;
    if (CASE.pins === 'auto' && seen && Array.isArray(seen.fields)) {
      pins = {'cc-csc': -1, 'cc-number': -1};
      for (const f of seen.fields) pins[f.autocomplete] = f.index;
    }
    result = api.resume(pins);
    continue;
  }
  paused.push([result.filled_fields, writes.length]);
  if (CASE.paused === 'hide' && result.filled_fields === 1) n.hidden = true;  // between the pause and resume()
  result = api.resume();
}
process.stdout.write(JSON.stringify({result, writes, paused, repins}));
"""


def _setter_harness(case: dict) -> dict:
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic commit closure")
    values = {"CASE": json.dumps(case), "INSPECT": sp._INSPECT}
    harness = re.sub(r"\bCASE\b|\bINSPECT\b", lambda m: values[m.group(0)], _SETTER_HARNESS)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


_NUMBER_STEP = {"token": "cc-number", "step": 2}


@pytest.mark.parametrize("focus, strict, writes, check", [
    ("none", True, ["c", "n"], None), ("cover", False, ["c", "n"], None),  # reviewed shapes keep today's behaviour
    ("hide", False, ["c"], {"reason": "field_hidden", "fields": ["cc-number"]}),
    ("hide_other", False, ["c"], {"reason": "field_hidden", "fields": ["cc-csc"]}),
    ("relabel", False, ["c"], {"reason": "fields_relabeled", "fields": ["cc-number"], "keys": ["label"]}),
    # a title (supplemental text, the descriptor's hint) is pinned like the label
    ("retitle", False, ["c"], {"reason": "fields_relabeled", "fields": ["cc-number"], "keys": ["hint"]}),
    ("swap", False, ["c"], {"reason": "fields_replaced", "fields": ["cc-number"]}),
    ("navigate", False, ["c"], {"reason": "navigated"}), ("cover", True, ["c"], {"reason": "hit_test"}),
    # the parent iframe turned transparent / the top URL rewritten by focus code (same-origin chain)
    ("parent_transparent", True, ["c"], {"reason": "frame_changed"}), ("top_replaced", True, ["c"], {"reason": "navigated"}),
    ("slow_checks", False, ["c"], None),  # the re-checks themselves crossed the deadline: no setter after it
])
@pytest.mark.parametrize("repin", [False, True])
def test_a_focus_handler_that_changes_the_pinned_set_stops_the_next_setter(focus, strict, writes, check, repin):
    """Design v2 A5: page code runs on focus; the field is written only if the whole pinned set still holds
    right before its setter (and, strict, the field is what a click at its centre would hit). The stop names the
    failed check with fixed codes only. With re-pin switched on, a re-render asks the supervisor for a re-pin first
    (declined here); switched off (the default), the closure never asks."""
    out = _setter_harness({"focus": focus, "strict": strict, "repin": repin})
    assert out["writes"] == writes and out["paused"] == []
    if len(writes) == 1:
        error_type = "payment_consent_expired" if focus == "slow_checks" else "payment_fields_changed"
        expected = {"success": False, "error_type": error_type, "filled_fields": 1, "partially_filled": True}
        if check:
            expected["check"] = {**_NUMBER_STEP, **check}
        assert out["result"] == expected
        repinnable = repin and check is not None and check["reason"] not in ("navigated", "frame_changed", "hit_test")
        assert out["repins"] == ([expected["check"]] if repinnable else [])
    else:
        assert out["result"]["success"] is True and out["repins"] == []


@pytest.mark.parametrize("focus, writes, repins", [
    ("swap", ["c", "n2"], ["fields_replaced"]),  # a fresh number input, same document, same token: written
    ("relabel", ["c", "n"], ["fields_relabeled"]),  # the same input re-described: written once re-pinned
    ("hide", ["c"], ["field_hidden"]),  # the number is never deferred (the closure refuses it too): stop
])
def test_a_re_render_is_written_only_after_an_accepted_re_pin(focus, writes, repins):
    """dlz (re-pin): the supervisor's answer moves the pins; every check then runs again before the setter, and a
    re-pinned field pauses after its focus for the owner-chain check though the fill is not strict (Codex r1 P1-1)."""
    out = _setter_harness({"focus": focus, "strict": False, "pins": "auto", "repin": True})
    off = _setter_harness({"focus": focus, "strict": False, "pins": "auto"})  # switched off: stops, never asks
    assert off["writes"] == ["c"] and off["repins"] == [] and off["paused"] == []
    assert off["result"]["error_type"] == "payment_fields_changed" and off["result"]["check"]["reason"] == repins[0]
    assert out["writes"] == writes and [c["reason"] for c in out["repins"]] == repins
    assert out["paused"] == ([[1, 1]] if len(writes) == 2 else [])
    if len(writes) == 2:
        assert out["result"] == {"success": True, "filled_fields": 2}
    else:
        assert out["result"]["error_type"] == "payment_fields_changed" and out["result"]["filled_fields"] == 1


@pytest.mark.parametrize("paused, writes", [("none", ["c", "n"]), ("hide", ["c"])])
def test_a_paced_commit_pauses_after_each_focus_and_rechecks_before_the_setter(paused, writes):
    """Codex A2 r3 #1 (closure side): paced, the commit stops after each focus with no setter run yet, and
    resume() re-runs the local checks first: a field hidden while the supervisor checked the parents is not
    written."""
    out = _setter_harness({"focus": "none", "strict": True, "paced": True, "paused": paused})
    assert out["paused"] == [[0, 0], [1, 1]] and out["writes"] == writes
    if paused == "none":
        assert out["result"] == {"success": True, "filled_fields": 2}
    else:
        assert out["result"] == {"success": False, "error_type": "payment_fields_changed", "filled_fields": 1,
                                 "partially_filled": True,
                                 "check": {**_NUMBER_STEP, "reason": "field_hidden", "fields": ["cc-number"]}}


class _PayPalPage(sp.PaymentSupervisionMixin):
    """Four PayPal card-field OOPIFs under the merchant page over a fake CDP. Every round trip costs
    ``latency`` virtual seconds; ``sp.time``/``sp.asyncio.sleep`` run on the same virtual clock, so the
    commit's own deadlines (consent, 20 s budget, renderer) apply as they would on a slow remote browser."""

    TOKENS = ("cc-name", "cc-exp", "cc-csc", "cc-number")

    def __init__(self, latency=0.18):
        import threading

        self.latency, self.now, self.calls, self.writes, self.mutate = latency, 0.0, 0, [], None
        self._active, self._page_session_id, self._state_lock = True, "page", threading.Lock()
        self.frames = {f"f{n}": [f"L{n}", _PAYPAL + "/smart/card-field/f", _PAYPAL] for n in range(4)}
        self.top_url, self.hidden, self.gone, self.covered = MERCHANT + "/checkout", set(), set(), set()
        self.on_focus, self.focused = None, []

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        self.now += self.latency
        self.calls += 1
        await asyncio.sleep(0)
        p = params or {}

        def node(fid):
            loader, url, origin = self.frames[fid]
            return {"frame": {"id": fid, "loaderId": loader, "url": url, "securityOrigin": origin}}

        def value(v):
            return {"result": {"result": {"value": v}}}

        if method == "Page.getFrameTree":
            if session_id == "page":
                return {"result": {"frameTree": {
                    "frame": {"id": "top", "loaderId": "LT", "url": self.top_url, "securityOrigin": MERCHANT},
                    "childFrames": [{"frame": {"id": f"f{n}", "url": "", "securityOrigin": ""}} for n in range(4)]}}}
            if session_id[1:] in self.gone:
                raise RuntimeError("target detached")
            return {"result": {"frameTree": node(session_id[1:])}}
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [{"type": "iframe", "targetId": f"f{n}"} for n in range(4)]}}
        if method == "DOM.getFrameOwner":
            return {"result": {"backendNodeId": p["frameId"]}}
        if method == "Page.createIsolatedWorld":
            return {"result": {"executionContextId": 1}}
        if method == "DOM.resolveNode":
            return {"result": {"object": {"objectId": "owner-" + p["backendNodeId"]}}}
        if method == "Runtime.callFunctionOn":
            fn = p["functionDeclaration"]
            args = [a.get("value", a.get("objectId")) for a in p.get("arguments", [])]
            if fn == sp._OWNERS_SHOWN:
                return value([o["objectId"][len("owner-"):] not in self.hidden for o in p["arguments"]])
            scope, hit = getattr(sp, "_OWNERS_SCOPE", None), getattr(sp, "_OWNER_HIT", None)
            if fn in (scope, hit):  # in the merchant page's pinned world
                if args[:2] != [self.top_url, MERCHANT]:
                    return value(None)
                if fn == scope:
                    return value([o[len("owner-"):] not in self.hidden for o in args[2:]])
                frame = p["objectId"][len("owner-"):]
                return value(None if frame in self.hidden | self.covered else [args[2] + 100, args[3] + 100])
            if "commit(" in fn:
                if self.now * 1000 >= args[1]:  # the renderer's own deadline (anchor 0)
                    return value({"success": False, "error_type": "payment_consent_expired"})
                if len(args) > 3 and args[3] is True:  # paced: focused, then paused before the setter
                    self.focused.append(p["objectId"])
                    if self.on_focus:
                        self.on_focus(self, p["objectId"])
                    return value({"pending": True, "filled_fields": 0, "x": 10, "y": 10})
                return self._write(p["objectId"])
            if "resume()" in fn:
                return self._write(p["objectId"])
            if "retained()" in fn:
                return value([{"token": self.TOKENS[int(p["objectId"][1:])], "state": "held", "length": 4}])
            return value(True)  # ancestry ownerDocument check, valid()
        return {"result": {}}

    def _write(self, obj):
        self.writes.append(obj)
        if self.mutate and len(self.writes) == 1:
            self.mutate(self)
        return {"result": {"result": {"value": {"success": True, "filled_fields": 1}}}}

    def run(self, monkeypatch, strict=False):
        """Inspect-time tree from the real discovery path, then one commit of four documents."""
        monkeypatch.setattr(sp, "time", SimpleNamespace(monotonic=lambda: self.now))

        async def vsleep(seconds):
            self.now += seconds
            await asyncio.sleep(0)

        monkeypatch.setattr(sp, "asyncio", SimpleNamespace(sleep=vsleep, ensure_future=asyncio.ensure_future,
                                                           shield=asyncio.shield))
        monkeypatch.setattr(sp, "_SETTLE_SECONDS", 2.0)
        sessions = {f"f{n}": f"sf{n}" for n in range(4)}
        tree = asyncio.run(self._payment_tree("page", sessions))
        docs = tuple(PaymentDocument(f"f{n}", f"sf{n}", f"o{n}", _PAYPAL, _controls(t), clock_anchor=0.0)
                     for n, t in enumerate(self.TOKENS))
        i = PaymentInspection("op", "page", "target", MERCHANT, docs, tree, 120.0, owner=self,
                              child_sessions=dict(sessions),
                              frame_owners={"top": ("page", [(f"f{n}", f"owner-f{n}") for n in range(4)])})
        self.now = self.calls = 0
        fills = [{"index": 0, "token": t, "value": "x", "document": n} for n, t in enumerate(self.TOKENS)]
        return asyncio.run(self._commit_payment(i, fills, until=sp._COMMIT_BUDGET_SECONDS, strict=strict))


def test_a_four_frame_split_fill_stays_well_inside_the_budget_at_180_ms_per_round_trip(monkeypatch):
    """Codex A2 r2 #4: between documents the pinned chain is re-checked without OOPIF rediscovery. With
    every CDP round trip at 180 ms (and the real 2 s settle), all four PayPal frames are written and verified
    with a wide margin to the 20 s commit budget; a full rescan per document did not reach the number."""
    page = _PayPalPage(latency=0.18)
    out = page.run(monkeypatch)
    assert out == {"success": True, "filled_fields": 4, "retention_verified": True}
    assert page.writes == ["o0", "o1", "o2", "o3"]
    # Measured: 13.70 s incl. the 2 s settle and the release pass (e330d50371: 13.16 s, next document only; base
    # 80848d4404: 11.36 s; a full rescan per document: 22.86 s, expired before the number). The check before
    # document k reads the k+1 receiving frames plus one owner call per parent: 3, 4, 5 round trips.
    assert page.now < 0.7 * sp._COMMIT_BUDGET_SECONDS, page.now


@pytest.mark.parametrize("change", ["owner_hidden", "frame_navigated", "frame_gone", "top_navigated",
                                    # Codex A2 r3 #2: the frame that already received its fields changed
                                    "earlier_owner_hidden", "earlier_frame_navigated", "earlier_frame_gone"])
def test_a_later_document_is_not_written_once_the_page_changed_after_the_first(monkeypatch, change):
    """Split checkout: the first document's handlers hide or navigate the next card frame (or the one just
    filled), detach it, or move the top page: the next document gets no write and the earlier one is reported."""
    def mutate(page):
        frame = "f0" if change.startswith("earlier_") else "f1"
        if change.endswith("owner_hidden"):
            page.hidden.add(frame)
        elif change.endswith("frame_navigated"):
            page.frames[frame][0] = "L-new"
        elif change.endswith("frame_gone"):
            page.gone.add(frame)
        else:
            page.top_url = MERCHANT + "/elsewhere"

    page = _PayPalPage(latency=0.0)
    page.mutate = mutate
    out = page.run(monkeypatch)
    assert out == {"success": False, "error_type": "payment_page_changed", "filled_fields": 1, "partially_filled": True}
    assert page.writes == ["o0"]


@pytest.mark.parametrize("on_focus", ["none", "own_iframe_transparent", "merchant_url_changed", "field_covered",
                                      "later_iframe_transparent"])
def test_a_strict_frame_fill_checks_the_owner_chain_from_the_parent_after_each_focus(monkeypatch, on_focus):
    """Codex A2 r3 #1: a cross-origin parent is out of the card frame's reach, and focusing the field can blur a
    merchant input whose handler makes the iframe transparent, covers it or rewrites the merchant URL. Strict
    fills pause after each focus; the supervisor checks the chain from the parent and the field is written only
    if it holds. Stops before that field's setter, earlier fields reported."""
    def focused(page, obj):
        frame = "f" + obj[1:]
        if on_focus == "own_iframe_transparent" and obj == "o0":
            page.hidden.add(frame)
        elif on_focus == "merchant_url_changed" and obj == "o0":
            page.top_url = MERCHANT + "/checkout?step=2"
        elif on_focus == "field_covered" and obj == "o0":
            page.covered.add(frame)
        elif on_focus == "later_iframe_transparent" and obj == "o1":
            page.hidden.add(frame)

    page = _PayPalPage(latency=0.18)
    page.on_focus = focused
    out = page.run(monkeypatch, strict=True)
    if on_focus == "none":
        assert out == {"success": True, "filled_fields": 4, "retention_verified": True}
        assert page.focused == page.writes == ["o0", "o1", "o2", "o3"]
        # Measured: 15.14 s at 180 ms per round trip (reviewed: 13.70 s; each paced field adds 2 round trips).
        assert page.now < 0.8 * sp._COMMIT_BUDGET_SECONDS, page.now
    elif on_focus == "later_iframe_transparent":
        assert out == {"success": False, "error_type": "payment_page_changed", "filled_fields": 1,
                       "partially_filled": True} and page.writes == ["o0"]
    else:
        assert out == {"success": False, "error_type": "payment_page_changed", "filled_fields": 0,
                       "partially_filled": False} and page.writes == []


def test_a_top_document_that_navigated_after_the_fill_is_reported():
    tree = (("top", "", "https://shop.example/checkout", MERCHANT, ""), ("card", "", "", _PAYPAL, "top"))

    class Supervisor(_OwnerSupervisor):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "Page.getFrameTree":
                return {"result": {"frameTree": {"frame": {"url": "https://shop.example/thanks"}}}}
            return await super()._cdp(method, params, session_id, timeout)

    doc = PaymentDocument("card", "sc", "oc", _PAYPAL, _controls("cc-number"))
    out = asyncio.run(Supervisor(lambda n, f: True)._commit_payment(
        _owned_inspection(tree, [doc]), [{"index": 0, "token": "cc-number", "value": "4242", "document": 0}]))
    assert out["success"] is True and out["navigated_after_fill"] is True


class _CancelSupervisor(sp.PaymentSupervisionMixin):
    """Real commit and release code over a fake CDP; ``stall`` holds the commit at freshness or at the
    first release long enough for commit_payment's budget to run out."""

    def __init__(self, stall, loop):
        self.stall, self._loop, self.released, self.detached = stall, loop, [], []

    async def _payment_is_current(self, i):
        if self.stall == "freshness":
            await asyncio.sleep(5)
        return True

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        if method == "Runtime.releaseObject":
            if self.stall == "cleanup" and not self.released:
                await asyncio.sleep(0.6)
            self.released.append(params["objectId"])
        elif method == "Target.detachFromTarget":
            self.detached.append(params["sessionId"])
        fn = params.get("functionDeclaration", "")
        if fn == sp._OWNERS_SHOWN:
            return {"result": {"result": {"value": [True] * len(params["arguments"])}}}
        if "commit(" in fn:
            return {"result": {"result": {"value": {"success": True, "filled_fields": 1}}}}
        if "retained()" in fn:
            return {"result": {"result": {"value": [{"token": "cc-number", "state": "held", "length": 4}]}}}
        return {"result": {}}


@pytest.mark.parametrize("stall", ["freshness", "cleanup"])
def test_a_timed_out_commit_still_releases_every_pinned_owner_and_child_session(stall, monkeypatch):
    import threading

    monkeypatch.setattr(sp, "_COMMIT_BUDGET_SECONDS", 0.3)
    tree = (("top", "", "", MERCHANT, ""), ("btn", "", "", _PAYPAL, "top"), ("card", "", "", _PAYPAL, "btn"))
    i = _owned_inspection(tree, [PaymentDocument("card", "sc", "oc", _PAYPAL, _controls("cc-number"))])
    i.child_sessions = {"btn": "s-btn", "card": "sc"}
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sup = _CancelSupervisor(stall, loop)
    try:
        out = sup.commit_payment(i, [{"index": 0, "token": "cc-number", "value": "4242", "document": 0}])
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
    assert out["error_type"] == "payment_transport_uncertain" and i.consumed
    # The caller skips its discard once consumed, so commit_payment itself must have finished the release.
    assert sorted(sup.released) == ["btn", "card", "oc"] and sorted(sup.detached) == ["s-btn", "sc"]


def test_commit_after_the_inspection_window_is_expired_not_a_changed_page():
    sup, i = _FakeSupervisor([]), _split_inspection()
    i.expires_at = 0
    assert asyncio.run(sup._commit_payment(i, [])) == {"success": False, "error_type": "payment_consent_expired"}
    assert i.consumed and not sup.writes


def test_gateway_consent_wait_honours_a_shorter_per_call_deadline(monkeypatch):
    from tools import approval_gateway_wait as gw

    seen = []

    def notify(data):
        seen.append(data["timeout_s"])
        raise RuntimeError("stop here")  # notify_failed: returns at once

    monkeypatch.setattr(gw._ctx, "_get_approval_timeout", lambda: 300)
    monkeypatch.setattr(gw._ctx, "_fire_approval_hook", lambda *a, **k: None)
    data = {"command": "c", "description": "d", "pattern_key": "mcp_elicitation", "pattern_keys": ["mcp_elicitation"]}
    for asked in (None, 37, 900):
        assert gw._await_gateway_decision("s-deadline", notify, dict(data), timeout_s=asked)["notify_failed"]
    assert seen == [300, 37, 300]  # a per-call deadline only ever shortens the wait


def test_not_retained_fill_carries_a_stop_message(monkeypatch):
    from tools import browser_vault_payment as bvp

    inspection = _four_hosted_documents()

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            return True

        def commit_payment(self, i, fills, strict=False):
            i.consumed = True
            return {"success": False, "error_type": "payment_fields_not_retained", "filled_fields": 2,
                    "partially_filled": True, "field_retention": {"cc-number": "empty"}, "field_lengths": {"cc-number": 0}}

        def discard_payment(self, i):
            pass

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: False)
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda task_id, surface=None: None)
    monkeypatch.setattr("agent.redact.register_vault_redaction_value", lambda value: None)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "accept")
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: dict(CARD), get_meta=lambda handle: meta)
    out = json.loads(bvp.fill_payment(backend, meta, "task"))
    assert out["success"] is False and out["submitted"] is False
    assert "do not retry" in out["error"] and "not a bank or issuer response" in out["error"]


@pytest.mark.parametrize("setup, verdict, reason", [
    ("gateway_no_callback", "decline", "unavailable"),
    ("gateway_notify_failed", "decline", "unavailable"),
    ("gateway_unanswered", "cancel", "timeout"),
    ("gateway_deny", "decline", "refused"),
    ("gateway_once", "accept", "accepted"),
    ("cli_no_user", "decline", "unavailable"),
    ("cli_timeout", "cancel", "timeout"),
    ("cli_never_shown", "cancel", "unavailable"),
    ("cli_deny", "decline", "refused"),
])
def test_elicitation_reports_why_consent_ended(monkeypatch, setup, verdict, reason):
    from tools import approval_prompt as ap

    gateway = setup.startswith("gateway")
    monkeypatch.setattr(ap._ctx, "get_current_session_key", lambda: "s")
    monkeypatch.setattr(ap._ctx, "_get_session_platform", lambda: "telegram" if gateway else "cli")
    monkeypatch.setattr(ap._ctx, "_is_gateway_approval_context", lambda: gateway)
    monkeypatch.setattr(ap._ctx, "_no_user_can_answer", lambda: setup == "cli_no_user")
    monkeypatch.setattr(ap._ctx, "_fire_approval_hook", lambda *a, **k: None)
    monkeypatch.setattr("tools.approval._gateway_notify_cb",
                        lambda key: None if setup == "gateway_no_callback" else (lambda *a: None))
    decisions = {"gateway_notify_failed": {"resolved": False, "notify_failed": True},
                 "gateway_unanswered": {"resolved": False}, "gateway_deny": {"resolved": True, "choice": "deny"},
                 "gateway_once": {"resolved": True, "choice": "once"}}
    monkeypatch.setattr(ap._gw, "_await_gateway_decision", lambda *a, **k: decisions[setup])
    choices = {"cli_timeout": "timeout", "cli_never_shown": ap.Unanswered("no_callback"), "cli_deny": "deny"}
    monkeypatch.setattr(ap, "prompt_dangerous_approval", lambda *a, **k: choices[setup])
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: None)
    detail = {}
    assert ap.request_elicitation_consent("m", "d", outcome=detail) == verdict
    assert detail == {"reason": reason}
    assert ap.request_elicitation_consent("m", "d") == verdict  # the verdict alone is unchanged


def test_coverage_refusal_names_an_unsupported_provider_but_other_refusals_stay_fixed():
    tree = (("page", "", MERCHANT + "/checkout", MERCHANT, ""),
            ("f1", "", "https://www.computop-paygate.com/paySSL.aspx", "https://www.computop-paygate.com", "page"))
    with pytest.raises(PaymentPolicyError,
                       match=r"^Incomplete or ambiguous card fields \(card frames on https://www\.computop-paygate\.com: Computop"):
        _explained_card_documents([_candidate(MERCHANT, "cc-name", frame="page")], MERCHANT, tree, ())
    with pytest.raises(PaymentPolicyError, match=r"^Card fields split across payment providers$"):
        _explained_card_documents(
            [_candidate(STRIPE, "cc-number"), _candidate("https://pay.datatrans.com", "cc-csc")], MERCHANT, tree, ())


def _select(index=0, **raw):
    return classify_checkout_control(LoginControl.from_dict({"index": index, "type": "select", **raw}))


@pytest.mark.parametrize("raw, token", [
    ({"autocomplete": "cc-exp-month"}, "cc-exp-month"), ({"autocomplete": "cc-exp-year"}, "cc-exp-year"),
    ({"name": "cardexpiremonth cardexpiremonth"}, "cc-exp-month"),  # PAYONE hosted iFrame
    ({"name": "cardexpireyear cardexpireyear"}, "cc-exp-year"),
    # A select is never the number, CVC, holder or a combined expiry, by token or by name.
    ({"autocomplete": "cc-number"}, None), ({"name": "cardpan"}, None), ({"autocomplete": "cc-csc"}, None),
    ({"label": "Cardholder"}, None), ({"label": "Expiry date"}, None),
    ({"name": "cardtype", "label": "Card type"}, None),  # PAYONE's merchant brand select
    ({"autocomplete": "country"}, "country-name"),  # address selects keep working
])
def test_a_select_is_only_ever_an_expiry_month_or_year(raw, token):
    c = _select(**raw)
    assert (c.token if c else None) == token


@pytest.mark.parametrize("name, token", [
    ("cardpan cardpan", "cc-number"), ("cardcvc2 cardcvc2", "cc-csc"), ("cardholder", "cc-name"),
    ("cardexpiremonth", "cc-exp-month"), ("cardexpireyear", "cc-exp-year"),
    ("pseudocardpan", None), ("truncatedcardpan", None),  # PAYONE's response fields are not inputs to fill
])
def test_payone_hosted_field_names(name, token):
    c = classify_checkout_control(LoginControl.from_dict({"index": 0, "type": "text", "name": name}))
    assert (c.token if c else None) == token


_MONTHS = [["", "MM", False]] + [[f"{m:02d}", f"{m:02d}", False] for m in range(1, 13)]


@pytest.mark.parametrize("token, value, options, want", [
    ("cc-exp-month", "07", _MONTHS, "07"),
    ("cc-exp-month", "07", [[str(m), name, False] for m, name in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                                                          "Jul"), 1)], "7"),
    ("cc-exp-month", "07", [["July", "07 - July", False]], None),  # text is not exactly the number
    ("cc-exp-month", "07", [[m, m, False] for m in ("January", "July")], None),  # names only
    ("cc-exp-month", "07", [[str(m), f"{m + 1:02d}", False] for m in range(12)], None),  # zero-based: 2 matches
    ("cc-exp-year", "2035", [["", "YYYY", False]] + [[str(y), str(y), False] for y in range(2026, 2041)], "2035"),
    ("cc-exp-year", "2035", [[f"{y}", f"{y}", False] for y in range(26, 41)], "35"),
    ("cc-exp-year", "2035", [[str(y), str(y), False] for y in range(2026, 2035)], None),  # year missing
    ("cc-exp-year", "2035", [["2035", "2035", False], ["35", "35", False]], None),  # ambiguous
    ("cc-exp-year", "2035", [["2035", "2035", True]], None),  # disabled
    ("cc-exp-year", "2035", [["2035", "2035", False], ["2035", "35", False]], None),  # duplicate value
    ("cc-number", "4242424242424242", [["4242424242424242", "", False]], None),
])
def test_expiry_option_matching(token, value, options, want):
    from agent.vault_login_classifier import match_expiry_option

    control = LoginControl.from_dict({"index": 0, "type": "select", "options": options})
    assert match_expiry_option(token, value, control.options) == want


def _payone_inspection(years):
    def doc(n, raw):
        return PaymentDocument(f"f{n}", f"s{n}", f"o{n}", "https://secure.pay1.de",
                               (classify_checkout_control(LoginControl.from_dict({"index": 0, **raw})),))
    return _inspection(
        doc(0, {"type": "text", "name": "cardpan"}), doc(1, {"type": "text", "name": "cardcvc2"}),
        doc(2, {"type": "select", "name": "cardexpiremonth", "options": _MONTHS}),
        doc(3, {"type": "select", "name": "cardexpireyear", "options": [[y, y, False] for y in years]}))


@pytest.mark.parametrize("years, outcome", [(["34", "35", "36"], "35"), (["2034", "2036"], None)])
def test_select_expiry_fill_maps_to_the_option_or_refuses_before_any_write(monkeypatch, years, outcome):
    from tools import browser_vault_payment as bvp

    inspection, committed = _payone_inspection(years), []

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            return True

        def commit_payment(self, i, fills, strict=False):
            committed.extend(fills)
            i.consumed = True
            return {"success": True, "filled_fields": len(fills)}

        def discard_payment(self, i):
            i.consumed = True

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: False)
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda task_id, surface=None: None)
    monkeypatch.setattr("agent.redact.register_vault_redaction_value", lambda value: None)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "accept")
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: dict(CARD), get_meta=lambda handle: meta)
    raw = bvp.fill_payment(backend, meta, "task")
    out = json.loads(raw)
    assert CARD["card_number"] not in raw and CARD["cvc"] not in raw
    if outcome is None:
        assert out["error_type"] == "payment_expiry_option_unmatched" and not committed and inspection.consumed
    else:
        assert out["success"] and {f["token"]: f["value"] for f in committed}["cc-exp-year"] == outcome
        assert {f["token"]: f["value"] for f in committed}["cc-exp-month"] == "07"


_PP = "https://www.paypal.com"
_PAYONE = "https://secure.pay1.de"


def _rows(*frames):
    """(id, loader, url, origin, parent) rows under a merchant page "m"."""
    return {r[0]: r for r in (("m", "", MERCHANT + "/checkout", MERCHANT, ""), *frames)}


@pytest.mark.parametrize("frames,target,expected", [
    # Captured 2026-11-03: PayPal's Buttons guest card form nests inside /smart/buttons.
    ((("b", "", _PP + "/smart/buttons?x=1", _PP, "m"), ("c", "", _PP + "/smart/card-fields?y", _PP, "b")), "c", True),
    ((("b", "", _PP + "/smart/buttons", _PP, "m"), ("a", "", "about:blank", _PP, "b"),
      ("c", "", _PP + "/smart/card-fields", _PP, "a")), "c", True),
    # The buttons page and PayPal's login/identity pages are never fill targets.
    ((("b", "", _PP + "/smart/buttons", _PP, "m"),), "b", False),
    ((("x", "", _PP + "/idapps/xauth/portal", _PP, "m"),), "x", False),
    # A card form under a PayPal login or identity page is refused, not filled.
    ((("x", "", _PP + "/idapps/xauth/portal", _PP, "m"), ("c", "", _PP + "/smart/card-fields", _PP, "x")), "c", "raise"),
    # PAYONE: only its hosted field document, directly under the merchant page.
    ((("p", "", _PAYONE + "/client-api/js/v1/payone_iframe.html", _PAYONE, "m"),), "p", True),
    ((("p", "", _PAYONE + "/frontend/other.html", _PAYONE, "m"),), "p", False),
])
def test_frame_admission_for_paypal_guest_form_and_payone(frames, target, expected):
    rows = _rows(*frames)
    if expected == "raise":
        with pytest.raises(PaymentPolicyError, match="Unapproved payment frame ancestry"):
            sp._frame_admitted(rows[target], rows, MERCHANT, (_PP, _PAYONE))
    else:
        assert sp._frame_admitted(rows[target], rows, MERCHANT, (_PP, _PAYONE)) is expected


def test_payone_checkout_shape_is_five_documents_with_merchant_holder():
    docs = _card_documents([
        _candidate(MERCHANT, "cc-name", frame="page"),
        _candidate(_PAYONE, "cc-number", frame="pan"), _candidate(_PAYONE, "cc-csc", frame="cvc"),
        _candidate(_PAYONE, "cc-exp-month", frame="mm"), _candidate(_PAYONE, "cc-exp-year", frame="yy"),
    ], MERCHANT)
    assert len(docs) == 5


# Round 4 (redesign): the new reach (separated holder, hint, document veto) counts only on positive evidence, and
# the holders base already filled are filled again. Regressions from the round-3 final reviews (Codex P1-P3, Opus
# M1, M2, L1, L2).
_M2_SEPARATED_LABELS = [
    "Card holder nationality", "Card holder IBAN", "Card holder contact", "Card holder initials",
    "Card holder reference", "Card holder signature", "Card holder title", "Card holder relationship",
    "Card holder account number", "Card holder number", "Card holder age", "Card holder VAT number",
    "Card holder tax number", "Card holder CPF", "Card holder social security number", "Card holder fax",
    "Card holder WhatsApp", "Card holder cell", "Card holder occupation", "Card holder employer",
    "Card holder house number", "Karteninhaber Hausnummer", "Karteninhaber Ort", "Karteninhaber Land",
    "Karteninhaber Geburtsdatum", "Titulaire de la carte téléphone", "Titular de la tarjeta teléfono",
    "Titular de la tarjeta DNI", "Titolare della carta telefono", "Kaarthouder telefoonnummer", "Kaarthouder adres",
]


@pytest.mark.parametrize("control, token", [
    # Codex P1: a hint never names a field whose own words are not generic ids ...
    *(({"name": "f1 ", "label": "ID", "hint": hint}, None) for hint in ("Card number", "CVC", "Expiry date", "Name on card")),
    ({"name": "f1 ", "label": "Contact number", "hint": "Card number"}, None),
    ({"name": "f1 f1", "label": "Phone", "hint": "Card number"}, None),
    ({"name": "phone1 ", "hint": "Card number"}, None), ({"name": "id1 ", "hint": "Card number"}, None),
    ({"name": " ", "autocomplete": "nope", "hint": "Card number"}, None),
    # ... and still names one whose own words are generated ids, digits or mask characters.
    ({"name": "f_3a9 ", "hint": "Card number"}, "cc-number"), ({"name": ":r1: input-12", "hint": "CVC"}, "cc-csc"),
    ({"name": "3f2a9c1e-8b7d-4c1a-9e2f-1a2b3c4d5e6f ", "hint": "Expiry date"}, "cc-exp"),
    ({"name": "el7 ", "label": "  1234 5678 9012 3456", "hint": "Card number"}, "cc-number"),
    # Opus L2: a group legend naming several card roles names none of the fields.
    ({"name": " ", "hint": "Enter card number, expiry date and security code Expiry date"}, None),
    ({"name": " ", "hint": "Card number, expiry date and CVC"}, None), ({"name": " ", "hint": "Card details Expiry date"}, None),
    # Codex P2: other holder attributes, separated and joined
    ({"label": "Card holder fax", "type": "tel"}, None), ({"label": "Card holder VAT number"}, None),
    ({"label": "Card holder title"}, None), ({"name": "cardholder ", "label": "Cardholder fax"}, None),
    ({"name": "cardholder ", "label": "Cardholder VAT number"}, None), ({"name": "cardholder ", "label": "Cardholder title"}, None),
    ({"name": "cardholder ", "label": "Cardholder house number"}, None),
    ({"name": "card-holder", "label": "Card holder initials"}, None),  # one neutral source is not enough
    # Opus M1: the holders base filled, with anti-autofill autocomplete, full-name placeholders, an alternative ...
    *(({"name": "cardholder ", "label": "Name on card", "autocomplete": ac}, "cc-name")
      for ac in ("nope", "chrome-off", "new-password", "false", "disabled", "none")),
    ({"name": "cardholder ", "label": "Cardholder name  Name Surname"}, "cc-name"),
    ({"name": "cardholder ", "label": "Cardholder name  Name and surname"}, "cc-name"),
    ({"name": "cardholder ", "label": "Cardholder name (or company name)"}, "cc-name"),
    ({"name": "cardholder ", "label": "Cardholder name (as on ID)"}, "cc-name"),
    ({"name": "card-holder ", "label": "Name Surname"}, "cc-name"),
    # ... while a recognised autocomplete token with another purpose, a company or a bare ID still conflict.
    *(({"name": "cardholder ", "label": "Name on card", "autocomplete": ac}, None)
      for ac in ("given-name", "cc-family-name", "tel-national", "organization", "honorific-prefix", "bday-day",
                 "current-password", "one-time-code", "username")),
    ({"name": "cardholder ", "label": "Cardholder company"}, None), ({"name": "cardholder ", "label": "Cardholder ID"}, None),
    # Codex P3, Opus L1: "Visa" is the brand, a DOM id is no document, a card field's own name is never vetoed
    ({"name": "cvc cvc", "label": "Visa CVC"}, "cc-csc"), ({"name": "f2 document-card-number", "label": "Card number"}, "cc-number"),
    ({"name": "cvv ", "label": "CVV (3 digits on the back of your Visa)"}, "cc-csc"),
    ({"label": "CVV (3 digits on the back of your Visa)"}, "cc-csc"),
    ({"name": "cvc ", "label": "Card identification number (CID)"}, "cc-csc"),
    ({"name": "exp ", "label": "Expiry date (Visa)"}, "cc-exp"), ({"label": "Expiry date (Visa)"}, "cc-exp"),
    ({"name": "cardnumber ", "label": "Card number (not your ID card number)"}, "cc-number"),
    # ... and the explicit document purposes still veto.
    ({"label": "Residence permit valid until"}, None), ({"name": "f1 ", "label": "Passport expiry"}, None),
    ({"label": "Driver's license expiry"}, None), ({"label": "Identity card valid until"}, None),
    ({"name": "documentExpiryDate ", "label": "Expiry date"}, None), ({"name": "docExpiry ", "label": "Expiry date"}, None),
    ({"name": "idExpiry ", "label": "Expiry date"}, None), ({"name": " expiry", "label": "Passport expiry"}, None),
    # A hint naming another purpose beside the card role names nothing.
    ({"name": " ", "hint": "Card number or phone"}, None), ({"name": " ", "hint": "Card number (ID number for business)"}, None),
    ({"name": " ", "hint": "Name on card or billing address"}, None),
    # A label naming another name beside a separated holder attribute; a recognised token with another purpose.
    ({"name": "card-holder", "label": "Maiden name"}, None), ({"name": "card-holder", "label": "Nickname"}, None),
    ({"name": "card-holder", "label": "Display name"}, None), ({"name": "card-holder", "label": "Firstname Lastname"}, "cc-name"),
    ({"name": "cardholder ", "label": "Name on card", "autocomplete": "nickname"}, None),
])
def test_round_4_new_reach_needs_positive_evidence(control, token):
    assert _token(**control) == token


@pytest.mark.parametrize("label", _M2_SEPARATED_LABELS)
def test_round_4_a_holder_attribute_is_never_the_holder(label):
    # Opus M2: a separated holder phrase beside another word never makes the holder, in any source combination.
    for extra in ({}, {"type": "tel"}, {"name": "card-holder"}):
        assert _token(label=label, **extra) != "cc-name", extra


# The holder's other attributes beside a complete card form: three writes, none into the attribute field.
@pytest.mark.parametrize("field", [
    *({"name": "", "id": "x", "label": label} for label in _M2_SEPARATED_LABELS),
    {"name": "", "id": "x", "label": "Card holder fax", "type": "tel"},
    *({"name": "cardholder", "id": "x", "label": label} for label in ("Cardholder fax", "Cardholder VAT number",
                                                                    "Cardholder title", "Cardholder house number")),
])
def test_round_4_holder_attribute_fields_never_receive_the_holder_name(field):
    fills, writes = _pipeline([_NUMBER, _EXPIRY, _CVC, field])
    assert sorted(f["token"] for f in fills) == ["cc-csc", "cc-exp", "cc-number"]
    assert len(writes) == 3 and all(element != "x" for element, _value in writes)


@pytest.mark.parametrize("label", ["ID", "Contact number"])
@pytest.mark.parametrize("attrs, texts", [
    *(({"title": role}, {}) for role in ("Card number", "CVC", "Expiry date", "Name on card")),
    ({"aria-labelledby": "role"}, {"role": "Card number"}),
])
def test_round_4_an_id_or_contact_input_titled_as_a_card_field_never_receives_it(label, attrs, texts):
    # Codex P1: beside a complete card form the suspect input is no card field (three writes, none into it) ...
    suspect = {"name": "f1", "id": "f1", "label": label, "attrs": attrs}
    fills, writes = _pipeline([suspect, _NUMBER, _EXPIRY, _CVC], texts)
    assert sorted(f["token"] for f in fills) == ["cc-csc", "cc-exp", "cc-number"]
    assert len(writes) == 3 and all(element != "f1" for element, _value in writes)
    # ... and in place of the number it does not complete the form.
    if attrs.get("title", "Card number") == "Card number":
        assert _pipeline([suspect, _EXPIRY, _CVC], texts) == ("Incomplete or ambiguous card fields", None)


_ZZ_HOLDERS = [
    {"name": "cardholder", "id": "h", "label": "Name on card", "ac": "nope"},
    {"name": "cardholder", "id": "h", "label": "Name on card", "ac": "chrome-off"},
    {"name": "cardholder", "id": "h", "label": "Name on card", "ac": "new-password"},
    {"name": "cardholder", "id": "h", "label": "Cardholder name", "placeholder": "Name Surname"},
    {"name": "cardholder", "id": "h", "label": "Cardholder name", "placeholder": "Name and surname"},
    {"name": "cardholder", "id": "h", "label": "Cardholder name (or company name)"},
    {"name": "cardholder", "id": "h", "label": "Cardholder name (as on ID)"},
    {"name": "", "id": "h", "label": "Card holder", "placeholder": ""},
]


@pytest.mark.parametrize("holder", _ZZ_HOLDERS)
def test_round_4_base_filled_holders_are_written_again(holder):
    # Opus M1 (adapted from the review's test_zz_review_holder_drop.py): round 3 dropped these; base wrote them.
    fills, writes = _pipeline([_NUMBER, _EXPIRY, _CVC, holder])
    assert [f["value"] for f in fills if f["token"] == "cc-name"] == [CARD["cardholder_name"]]
    assert ["h", CARD["cardholder_name"]] in writes and len(writes) == 4


@pytest.mark.parametrize("fields, element, value", [
    ([_NUMBER, _EXPIRY, {"name": "cvc", "id": "v", "label": "Visa CVC"}], "v", CARD["cvc"]),
    ([_NUMBER, _EXPIRY, {"name": "cvv", "id": "v", "label": "CVV (3 digits on the back of your Visa)"}], "v", CARD["cvc"]),
    ([_NUMBER, _EXPIRY, {"name": "cvc", "id": "v", "label": "Card identification number (CID)"}], "v", CARD["cvc"]),
    ([{"name": "", "id": "document-card-number", "label": "Card number"}, _EXPIRY, _CVC], "document-card-number", _PAN),
    ([_NUMBER, {"name": "exp", "id": "v", "label": "Expiry date (Visa)"}, _CVC], "v", None),
])
def test_round_4_visa_branded_and_document_id_card_fields_are_written(fields, element, value):
    # Codex P3, Opus L1: round 3 vetoed these as documents, and the complete form was refused.
    _fills, writes = _pipeline(fields)
    assert len(writes) == 3 and element in [e for e, _v in writes]
    if value is not None:
        assert [element, value] in writes


def test_round_4_a_group_legend_in_aria_labelledby_names_no_field():
    # Opus L2: three hosted inputs whose aria-labelledby is the group legend plus their own label: the legend names
    # every card role, so no field is classified from it and the form is refused before any write ...
    legend = "Enter card number, expiry date and security code"
    texts = {"legend": legend, "ln": "Card number", "le": "Expiry date", "lc": "Security code"}
    fields = [{"name": "", "id": f"i{n}", "attrs": {"aria-labelledby": f"legend {own}"}}
              for n, own in enumerate(("ln", "le", "lc"))]
    result, writes = _pipeline(fields, texts)
    assert isinstance(result, str) and writes is None
    # ... while their own labels alone name each field.
    fields = [{**f, "attrs": {"aria-labelledby": own}} for f, own in zip(fields, ("ln", "le", "lc"))]
    _fills, writes = _pipeline(fields, texts)
    assert ["i0", _PAN] in writes and ["i2", CARD["cvc"]] in writes and len(writes) == 3

