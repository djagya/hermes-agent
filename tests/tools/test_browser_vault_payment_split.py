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
from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection, _card_documents

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

    async def _commit_payment_document(self, d, fills):
        self.writes.append((d.origin, fills))
        return self.results.pop(0)

    async def _payment_retention(self, d):
        fills = next(f for origin, f in self.writes if origin == d.origin)
        if self.retained.get(d.origin) == "gone":
            return None
        return [{"token": f.get("token", ""), "state": self.retained.get(f.get("token"), "held"),
                 "length": 0 if self.retained.get(f.get("token")) == "empty" else len(f["value"])} for f in fills]

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

        def commit_payment(self, i, fills):
            committed.extend(fills)
            i.consumed = True
            return {"success": True, "filled_fields": len(fills)}

        def discard_payment(self, i):
            pass

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda task_id, surface=None: None)
    monkeypatch.setattr("agent.redact.register_vault_redaction_value", registered.append)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent",
                        lambda *args, **k: consent_text.append(args[1]) or "accept")
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: dict(CARD))
    meta = SimpleNamespace(id="h", label="Card", allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)

    out = json.loads(bvp.fill_payment(backend, meta, "task"))
    assert out["success"] and out["target_origin"] == DATATRANS and out["target_origins"] == [MERCHANT, DATATRANS]
    assert f"{MERCHANT}, {DATATRANS}" in consent_text[0]
    assert sorted((f["document"], f["token"]) for f in committed) == [
        (0, "cc-exp-month"), (0, "cc-exp-year"), (0, "cc-name"), (1, "cc-number"), (2, "cc-csc")]
    assert {"4242424242424242", "4242 4242 4242 4242", "4242-4242-4242-4242", "837"} <= set(registered)
    assert not {"07", "2035", "35", "Ada Lovelace"} & set(registered), registered


def test_supervisor_attach_errors_never_reach_the_result(monkeypatch):
    from tools import browser_vault_payment as bvp

    def boom(task_id):
        raise RuntimeError("ws://127.0.0.1:9222/devtools/browser/secret-token")

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", boom)
    raw = bvp.fill_payment(SimpleNamespace(), SimpleNamespace(), "task")
    assert json.loads(raw) == {"success": False, "error_type": "supervisor_required"} and "secret-token" not in raw


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


def _fill_with_consent(monkeypatch, verdict, reason):
    from tools import browser_vault_payment as bvp

    inspection = _four_hosted_documents()
    calls = []

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            calls.append("payment_is_current")
            return True

        def commit_payment(self, i, fills):
            calls.append("commit_payment")
            return {"success": True}

        def discard_payment(self, i):
            calls.append("discard_payment")

    def consent(*args, outcome=None, **kwargs):
        if outcome is not None and reason:
            outcome["reason"] = reason
        return verdict

    def resolve(handle):
        calls.append("resolve_secret")
        return dict(CARD)

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session",
                        lambda task_id, surface=None: calls.append("mark_payment_session"))
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", consent)
    meta = SimpleNamespace(id="h", label="Card", allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)
    raw = bvp.fill_payment(SimpleNamespace(name="local", resolve_secret=resolve), meta, "task")
    return json.loads(raw), calls


@pytest.mark.parametrize("verdict, reason, expected", [
    ("decline", "refused", "refused"), ("cancel", "timeout", "timeout"), ("decline", "unavailable", "unavailable"),
    ("decline", None, "refused"), ("cancel", None, "timeout"),  # a consent stub without a reason
])
def test_consent_not_given_stops_before_any_secret_or_write(monkeypatch, verdict, reason, expected):
    out, calls = _fill_with_consent(monkeypatch, verdict, reason)
    # Same error_type as before (callers stop and never retry on it), now with the typed reason.
    assert out["success"] is False and out["error_type"] == "payment_declined"
    assert out["consent_outcome"] == expected and out["issuer_response"] is False
    assert "not a bank or issuer decline" in out["error"]
    assert calls == ["discard_payment"]  # no freshness pass, no secret, no capture mark, no write


def test_not_retained_fill_carries_a_stop_message(monkeypatch):
    from tools import browser_vault_payment as bvp

    inspection = _four_hosted_documents()

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            return True

        def commit_payment(self, i, fills):
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
    meta = SimpleNamespace(id="h", label="Card", allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: dict(CARD))
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
