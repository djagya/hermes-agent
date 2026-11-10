"""Card-fill approvals resolve only from the owner's own button (payment design v2 A2/A3, Codex #2/#8/#16).

An exact request is selected only by its own minted id and accepts only ``once``; an approver-bound
request accepts a non-deny choice only from that approver. Every other resolver (typed /approve,
/approve all, TUI, pending controls, api_server) passes no approver and can only deny.
"""

import threading

import pytest

from tools import approval, approval_prompt
from tools.approval_gateway_wait import _ApprovalEntry, _await_gateway_decision
from tools.payment_consent_prompt import PaymentPromptFacts, ReceivingOrigin, UNAVAILABLE_APPROVER, \
    UNAVAILABLE_SURFACE, request_payment_approval

SESSION = "agent:main:telegram:group:-100:1"
OWNER = "telegram:111"


@pytest.fixture(autouse=True)
def _clean_queues():
    yield
    with approval._lock:
        approval._gateway_queues.pop(SESSION, None)
        approval._gateway_notify_cbs.pop(SESSION, None)


def _queue_payment(**data):
    entry = _ApprovalEntry({"command": "fill", "exact_request": True, "approver": OWNER, "request_id": "", **data})
    with approval._lock:
        approval._gateway_queues.setdefault(SESSION, []).append(entry)
    return entry


def _facts():
    return PaymentPromptFacts(ref="a" * 32, card_label="Test card", merchant_origin="https://shop.example",
                              receiving=(ReceivingOrigin("https://shop.example", "merchant", ("cc-number", "cc-csc")),),
                              expires_in_s=90)


def test_exact_request_mints_its_own_id_and_rejects_empty_or_foreign_ids():
    entry = _queue_payment(request_id="")  # a caller-supplied empty id is replaced
    rid = entry.data["request_id"]
    assert approval.valid_request_id(rid) == rid
    for bad in ("", " ", None, "not-hex", "b" * 32, rid.upper()):
        assert approval.resolve_gateway_approval(SESSION, "once", request_id=bad, approver=OWNER) == 0
    for scope in ("session", "always"):
        assert approval.resolve_gateway_approval(SESSION, scope, request_id=rid, approver=OWNER) == 0
    assert entry.result is None and approval.has_blocking_approval(SESSION)
    assert approval.resolve_gateway_approval(SESSION, "once", request_id=rid, approver=OWNER) == 1
    assert entry.result == "once"


def test_every_resolver_but_the_owner_can_only_deny():
    entry = _queue_payment()
    rid = entry.data["request_id"]
    # Another Telegram user (an authorized group member), typed /approve, /approve all, and the
    # request-id resolvers without a principal (TUI, pending controls, api_server, managed worker).
    assert approval.resolve_gateway_approval(SESSION, "once", request_id=rid, approver="telegram:222") == 0
    assert approval.resolve_gateway_approval(SESSION, "once") == 0
    assert approval.resolve_gateway_approval(SESSION, "always", resolve_all=True) == 0
    assert approval.resolve_gateway_approval(SESSION, "once", request_id=rid) == 0
    assert entry.result is None
    assert approval.resolve_gateway_approval(SESSION, "deny") == 1  # FIFO /deny from anyone
    assert entry.result == "deny"


def test_fifo_approve_skips_the_payment_and_takes_the_next_ordinary_prompt():
    payment = _queue_payment()
    ordinary = _ApprovalEntry({"command": "rm -rf build"})
    with approval._lock:
        approval._gateway_queues[SESSION].append(ordinary)
    assert approval.resolve_gateway_approval(SESSION, "once") == 1
    assert ordinary.result == "once" and payment.result is None


def test_identical_exact_requests_are_never_coalesced():
    notified = []
    results = []

    def ask():
        results.append(_await_gateway_decision(SESSION, notified.append,
                                               {"command": "fill", "exact_request": True, "approver": OWNER},
                                               timeout_s=10))

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for th in threads:
        th.start()
    from tests.waiting import wait_until
    assert wait_until(lambda: len(notified) == 2, timeout=5)
    ids = {data["request_id"] for data in notified}
    assert len(ids) == 2
    for rid in ids:
        assert approval.resolve_gateway_approval(SESSION, "once", request_id=rid, approver=OWNER) == 1
    for th in threads:
        th.join(5)
    assert [r["choice"] for r in results] == ["once", "once"]


@pytest.mark.parametrize("configured, allowed, expected", [
    ("", "111", None),
    ("telegram:abc", "111", None),
    ("discord:111", "111", None),
    ("telegram:111", "222", None),
    ("telegram:111", "*", None),  # a wildcard does not name the owner
    ("telegram:111", "222, 111", OWNER),
])
def test_payment_approver_must_be_a_listed_telegram_user(monkeypatch, configured, allowed, expected):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"payment_approver": configured})
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", allowed)
    assert approval_context.payment_approver() == expected


def _gateway_session(monkeypatch, *, approver=OWNER, gateway=True):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "payment_approver", lambda: approver)
    monkeypatch.setattr(approval_context, "get_current_session_key", lambda default="default": SESSION)
    monkeypatch.setattr(approval_context, "_no_user_can_answer", lambda: False)
    monkeypatch.setattr(approval_context, "_is_gateway_approval_context", lambda: gateway)


class _CardNotifier:
    renders_payment_cards = True

    def __init__(self, answer=None):
        self.sent, self.answer = [], answer

    def __call__(self, data):
        self.sent.append(data)
        if self.answer:
            approver, choice = self.answer
            threading.Thread(target=approval.resolve_gateway_approval, args=(SESSION, choice),
                             kwargs={"request_id": data["request_id"], "approver": approver}).start()


@pytest.mark.parametrize("setup, notifier", [
    ("no_approver", _CardNotifier()),
    ("cli", _CardNotifier()),
    ("plain_notifier", lambda data: None),  # TUI / api_server / managed worker: cannot render the card
])
def test_payment_prompt_is_unavailable_before_anything_is_queued(monkeypatch, setup, notifier):
    _gateway_session(monkeypatch, approver=None if setup == "no_approver" else OWNER, gateway=setup != "cli")
    approval.register_gateway_notify(SESSION, notifier)
    consent = request_payment_approval(_facts(), timeout_seconds=5)
    assert (consent.outcome, consent.reason) == ("unavailable", "unavailable")
    assert consent.detail == (UNAVAILABLE_APPROVER if setup == "no_approver" else UNAVAILABLE_SURFACE)
    assert not approval.has_blocking_approval(SESSION)
    assert not getattr(notifier, "sent", [])


@pytest.mark.parametrize("answer, outcome", [
    ((OWNER, "once"), "accept"),
    ((OWNER, "deny"), "deny"),
    (("telegram:222", "once"), "timeout"),  # another user's press leaves it pending until it expires
])
def test_only_the_owners_press_accepts(monkeypatch, answer, outcome):
    _gateway_session(monkeypatch)
    notifier = _CardNotifier(answer)
    approval.register_gateway_notify(SESSION, notifier)
    consent = request_payment_approval(_facts(), timeout_seconds=2 if outcome == "timeout" else 10)
    assert consent.outcome == outcome
    (data,) = notifier.sent
    assert data["exact_request"] and data["approver"] == OWNER and data["allow_session"] is False
    assert PaymentPromptFacts.from_dict(data["payment"]) == _facts()


def test_vault_payment_surface_without_facts_never_prompts(monkeypatch):
    _gateway_session(monkeypatch)
    notifier = _CardNotifier()
    approval.register_gateway_notify(SESSION, notifier)
    assert approval_prompt.request_elicitation_consent("Fill card?", "pay", surface="vault-payment") == "decline"
    assert not notifier.sent


def test_reviewed_fill_goes_through_the_payment_card_and_reads_nothing_without_an_approver(monkeypatch):
    """Today's reviewed fill uses request_payment_approval: facts come from the inspection (every
    receiving origin, reviewed flag, fields), and with no approver configured nothing is resolved."""
    import json
    import time
    from types import SimpleNamespace

    from agent.vault_login_classifier import LoginControl, classify_checkout_control
    from agent.vault_payment_policy import PAYMENT_PSP_ORIGINS
    from tools import browser_vault_payment as bvp
    from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection
    from tools.payment_consent_prompt import facts_for_inspection

    def controls(*tokens):
        return tuple(classify_checkout_control(LoginControl.from_dict({"index": n, "autocomplete": t}))
                     for n, t in enumerate(tokens))

    merchant, psp = "https://shop.example", "https://pay.datatrans.com"
    inspection = PaymentInspection("e" * 32, "page", "target", merchant, (
        PaymentDocument("f0", "s0", "o0", merchant, controls("cc-name")),
        PaymentDocument("f1", "s1", "o1", psp, controls("cc-number", "cc-csc"))), (), time.monotonic() + 100)
    facts = facts_for_inspection(inspection, card_label="Test card")
    assert [(r.origin, r.status, r.fields) for r in facts.receiving] == [
        (merchant, "merchant", ("cc-name",)), (psp, "reviewed", ("cc-number", "cc-csc"))]
    assert facts.ref == "e" * 32 and facts.cart is None and 1 <= facts.expires_in_s <= 100

    _gateway_session(monkeypatch, approver=None)
    resolved = []
    supervisor = SimpleNamespace(inspect_payment=lambda *a, **k: inspection, discard_payment=lambda i: None,
                                 payment_is_current=lambda i: True)
    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: supervisor)
    meta = SimpleNamespace(id="h", label="Test card", allowed_origins=(), origin=None, payment_scope="confirmed_page",
                           payment_frame_origins=PAYMENT_PSP_ORIGINS)
    out = json.loads(bvp.fill_payment(SimpleNamespace(name="local", resolve_secret=resolved.append), meta, "task"))
    assert out["error_type"] == "payment_consent_missing" and out["consent_outcome"] == "unavailable"
    assert "approvals.payment_approver" in out["error"] and not resolved


def test_a_local_viewer_is_offered_only_deny_and_cannot_approve():
    from gateway.session_events import SessionEvents
    from gateway.session_pending_controls import PendingControls
    from hermes_state_runtime import RuntimeStoreError

    entry = _queue_payment()
    rid = entry.data["request_id"]
    controls = PendingControls(SessionEvents())
    controls.register("viewer-session", SESSION, 1, entry.data)
    (prompt,) = controls.snapshot("viewer-session", 1)
    assert prompt["choices"] == ["deny"]
    with pytest.raises(RuntimeStoreError):
        controls.respond("viewer-session", 1, rid, {"choice": "once"})
    assert entry.result is None
    assert controls.respond("viewer-session", 1, rid, {"choice": "deny"})["status"] == "resolved"
    assert entry.result == "deny"
