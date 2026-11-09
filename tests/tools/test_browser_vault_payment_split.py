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

    async def _payment_tree(self, sid, child_sessions=None):
        return ()  # the synthetic inspections' tree

    async def _commit_payment_document(self, d, fills, deadline_ms=None, strict=False):
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
    assert {"4242424242424242", "4242 4242 4242 4242", "4242-4242-4242-4242", "837"} <= set(registered)
    assert not {"07", "2035", "35", "Ada Lovelace"} & set(registered), registered


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
    assert out["error_type"] == "payment_consent_expired" and "120 s inspection window" in out["error"]
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

        async def _commit_payment_document(self, d, fills, deadline_ms=None, strict=False):
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
        self.tree = i.tree
        return True

    async def _payment_tree(self, sid, child_sessions=None):
        return self.tree

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
    # dlz: settle before/after (2) plus one visibility pass before each later document (3, design v2 A5).
    assert sup.passes == 2 + 3
    assert sup.calls.count("Runtime.callFunctionOn") == 5 + 4 + 4  # owner checks + commits + retention reads


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
let writes = [], els, cover = null;
const performance = {now: () => 1000};
class HTMLInputElement { get value() { return this.held || ''; } set value(v) { writes.push(this.id); this.held = v; } }
class HTMLSelectElement { get value() { return ''; } set value(v) {} }
class Event {} class InputEvent extends Event {} class FocusEvent extends Event {}
const document = {forms: []};
const location = {href: 'https://psp.example/card'};
const mk = (id, token, top) => Object.assign(new HTMLInputElement(), {tagName: 'INPUT', autocomplete: token, name: id,
  id, labels: null, placeholder: '', type: 'text', form: null, maxLength: 0, disabled: false, readOnly: false,
  isConnected: true, tabIndex: 0, shadowRoot: null, ownerDocument: document, hidden: false, top,
  style: {setProperty() {}}, hasAttribute: () => false, dispatchEvent: () => true, blur() {},
  getAttribute(n) { return n === 'aria-label' ? (this.aria || null) : null; },
  getBoundingClientRect() { return this.hidden ? {width: 0, height: 0, left: 0, top: 0} : {width: 100, height: 20, left: 0, top: this.top}; },
  getClientRects() { return this.hidden ? [] : [1]; }, getRootNode: () => document,
  focus() { if (this.onfocus) this.onfocus(); }});
els = [mk('c', 'cc-csc', 0), mk('n', 'cc-number', 40)];
document.querySelectorAll = () => els;
document.elementFromPoint = (x, y) => cover || els.find(e => !e.hidden && y >= e.top && y < e.top + 20) || null;
const getComputedStyle = e => ({visibility: e.hidden ? 'hidden' : 'visible', opacity: '1', pointerEvents: 'auto'});
const api = INSPECT;
const n = els[1];
n.onfocus = {none: () => {}, hide: () => { n.hidden = true; }, hide_other: () => { els[0].hidden = true; },
  relabel: () => { n.aria = 'Gift card'; }, swap: () => { els = [els[0], mk('n2', 'cc-number', 40)]; },
  navigate: () => { location.href = 'https://psp.example/elsewhere'; }, cover: () => { cover = {}; }}[CASE.focus];
const result = api.commit([{index: 0, token: 'cc-csc', value: '837'}, {index: 1, token: 'cc-number', value: '4242'}],
                          60000, CASE.strict);
process.stdout.write(JSON.stringify({result, writes}));
"""


@pytest.mark.parametrize("focus, strict, writes", [
    ("none", True, ["c", "n"]), ("cover", False, ["c", "n"]),  # reviewed shapes keep today's behaviour
    ("hide", False, ["c"]), ("hide_other", False, ["c"]), ("relabel", False, ["c"]), ("swap", False, ["c"]),
    ("navigate", False, ["c"]), ("cover", True, ["c"]),
])
def test_a_focus_handler_that_changes_the_pinned_set_stops_the_next_setter(focus, strict, writes):
    """Design v2 A5: page code runs on focus; the field is written only if the whole pinned set still holds
    right before its setter (and, strict, the field is what a click at its centre would hit)."""
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic commit closure")
    values = {"CASE": json.dumps({"focus": focus, "strict": strict}), "INSPECT": sp._INSPECT}
    harness = re.sub(r"\bCASE\b|\bINSPECT\b", lambda m: values[m.group(0)], _SETTER_HARNESS)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["writes"] == writes
    if len(writes) == 1:
        assert out["result"] == {"success": False, "error_type": "payment_fields_changed", "filled_fields": 1,
                                 "partially_filled": True}
    else:
        assert out["result"]["success"] is True


@pytest.mark.parametrize("change", ["hidden", "tree", "unreadable"])
def test_a_later_document_is_not_written_once_the_page_changed_after_the_first(change):
    """Split checkout: the first document's handlers hide the next card frame or change the frame tree;
    the next document gets no write and the earlier one is reported."""
    class Supervisor(_FakeSupervisor):
        async def _payment_tree(self, sid, child_sessions=None):
            if self.writes and change == "unreadable":
                raise RuntimeError("frame gone")
            return (("x",),) if self.writes and change == "tree" else ()

        async def _shown_documents(self, i):
            return {"a"} if self.writes and change == "hidden" else {d.frame_id for d in i.documents}

    sup = Supervisor([{"success": True, "filled_fields": 1}] * 2)
    out = asyncio.run(sup._commit_payment(_split_inspection(), [
        {"index": 0, "token": "cc-csc", "value": "837", "document": 0},
        {"index": 0, "token": "cc-number", "value": "4242", "document": 1}]))
    assert out == {"success": False, "error_type": "payment_page_changed", "filled_fields": 1, "partially_filled": True}
    assert len(sup.writes) == 1


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
