"""Unbound card fill: confirm the charge, write only into the merchant page or Stripe, never echo the PAN."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.payment_grant import reset_for_tests
from agent.redact import clear_vault_redaction_values
from agent.vault_store import VaultItemMeta
from tools.browser_supervisor import CDPSupervisor
from tools.browser_vault_payment import (
    STRIPE_FRAME_ORIGIN,
    classify_embedded_frames,
    fill_unbound_card,
)

_PAN = "4111111111111111"
_CARD = {
    "card_number": _PAN,
    "cardholder_name": "A User",
    "exp_month": "10",
    "exp_year": "2027",
    "cvc": "123",
}
_ITEMS = [{"label": "General Admission", "count": 3}]
_STRIPE = "https://js.stripe.com/v3/elements-inner-card.html?componentName=card"


class _Backend:
    name = "onepassword"
    display_name = "1Password"
    needs_unlock = True

    def __init__(self):
        self.resolves = 0

    def resolve_secret(self, handle):
        self.resolves += 1
        return dict(_CARD)

    def get_meta(self, handle):
        return VaultItemMeta(
            id=handle,
            kind="payment",
            label="N26 Sera Credit Card",
            origin=None,
            created_at="",
        )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_for_tests()
    clear_vault_redaction_values()
    yield
    reset_for_tests()
    clear_vault_redaction_values()


def _fill(
    backend,
    *,
    eval_js,
    secret_eval,
    supervisor=None,
    origin="https://tickets.mahalla.berlin",
    consent="accept",
    submit=False,
    consent_hook=None,
):
    asks = []

    def _consent(message, description, **_kwargs):
        asks.append(message)
        if consent_hook:
            consent_hook()
        return consent

    with (
        patch("tools.browser_vault_tool._focus_bound_origin", return_value=None),
        patch("tools.browser_vault_tool._current_page_origin", return_value=origin),
        patch("tools.browser_vault_tool._eval_js", side_effect=eval_js),
        patch("tools.browser_vault_tool._eval_js_secret", side_effect=secret_eval),
        patch("tools.browser_vault_tool._ensure_supervisor", return_value=supervisor),
        patch(
            "tools.approval_prompt.request_elicitation_consent", side_effect=_consent
        ),
    ):
        raw = fill_unbound_card(
            handle="op:vault:card",
            label="N26 Sera Credit Card",
            backend=backend,
            task_id="task",
            currency="EUR",
            amount="94.26",
            recipient="owner@example.com",
            items=_ITEMS,
            submit=submit,
        )
    return raw, json.loads(raw), asks


def _page_eval(controls):
    def eval_js(task_id, expression):
        if "data-hermes-vault-slot" in expression:
            return {"success": True, "result": json.dumps(controls)}
        if "ambiguous_submit" in expression:
            return {"success": True, "result": json.dumps({"clicked": True})}
        if "threeDS" in expression:
            return {
                "success": True,
                "result": json.dumps({"declined": False, "threeDS": False}),
            }
        if "iframe" in expression:
            return {"success": True, "result": json.dumps([])}
        return {"success": False, "error": "unexpected eval"}

    return eval_js


_PAGE_CONTROLS = [
    {"autocomplete": "cc-number", "index": 0, "type": "text"},
    {"autocomplete": "cc-exp", "index": 1, "type": "text"},
    {"autocomplete": "cc-csc", "index": 2, "type": "text"},
]


def test_decline_and_missing_envelope_write_nothing():
    backend = _Backend()
    secrets = []
    raw, declined, _asks = _fill(
        backend,
        eval_js=_page_eval(_PAGE_CONTROLS),
        secret_eval=lambda *args, **kwargs: secrets.append(args),
        consent="decline",
    )
    assert declined["error_type"] == "payment_declined"
    assert backend.resolves == 0 and secrets == [] and _PAN not in raw

    with (
        patch("tools.browser_vault_tool._focus_bound_origin", return_value=None),
        patch(
            "tools.browser_vault_tool._current_page_origin",
            return_value="https://tickets.mahalla.berlin",
        ),
    ):
        parsed = json.loads(
            fill_unbound_card(
                handle="op:vault:card",
                label="N26",
                backend=backend,
                task_id="task",
                currency=None,
                amount=None,
                recipient=None,
                items=None,
                submit=False,
            )
        )
    assert parsed["error_type"] == "payment_envelope_invalid"
    assert backend.resolves == 0


def test_page_form_fills_after_one_confirmation_and_hides_the_pan():
    backend = _Backend()
    secret_calls = []

    def secret_eval(task_id, expression, frame_session=None):
        secret_calls.append((frame_session, expression))
        return {"success": True, "result": json.dumps({"filled": 3})}

    raw, body, asks = _fill(
        backend, eval_js=_page_eval(_PAGE_CONTROLS), secret_eval=secret_eval
    )
    assert body["success"] is True
    assert body["provider"] == "page" and body["outcome"] == "filled"
    assert body["merchant_acceptance"] == "unverified"
    assert body["authorization"] == "confirmed"
    assert _PAN not in raw and "123" not in json.dumps({
        k: v for k, v in body.items() if k != "fields"
    })
    assert secret_calls and secret_calls[0][0] is None
    assert (
        _PAN in secret_calls[0][1]
        and "https://tickets.mahalla.berlin" in secret_calls[0][1]
    )
    assert len(asks) == 1

    raw2, body2, asks2 = _fill(
        backend, eval_js=_page_eval(_PAGE_CONTROLS), secret_eval=secret_eval
    )
    assert body2["authorization"] == "reused" and asks2 == []


def test_amount_change_and_navigation_do_not_reuse_the_grant():
    backend = _Backend()

    def secret_eval(task_id, expression, frame_session=None):
        return {"success": True, "result": json.dumps({"filled": 3})}

    _fill(backend, eval_js=_page_eval(_PAGE_CONTROLS), secret_eval=secret_eval)
    asks = []

    def consent(message, description, **_kwargs):
        asks.append(message)
        return "accept"

    with (
        patch("tools.browser_vault_tool._focus_bound_origin", return_value=None),
        patch(
            "tools.browser_vault_tool._current_page_origin",
            return_value="https://tickets.mahalla.berlin",
        ),
        patch(
            "tools.browser_vault_tool._eval_js", side_effect=_page_eval(_PAGE_CONTROLS)
        ),
        patch("tools.browser_vault_tool._eval_js_secret", side_effect=secret_eval),
        patch("tools.browser_vault_tool._ensure_supervisor", return_value=None),
        patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent),
    ):
        raw = fill_unbound_card(
            handle="op:vault:card",
            label="N26 Sera Credit Card",
            backend=backend,
            task_id="task",
            currency="EUR",
            amount="10.00",
            recipient="owner@example.com",
            items=_ITEMS,
            submit=False,
        )
    assert json.loads(raw)["authorization"] == "confirmed" and len(asks) == 1

    origins = ["https://tickets.mahalla.berlin", "https://evil.test"]

    def origin(_task):
        return origins.pop(0)

    with (
        patch("tools.browser_vault_tool._focus_bound_origin", return_value=None),
        patch("tools.browser_vault_tool._current_page_origin", side_effect=origin),
        patch(
            "tools.browser_vault_tool._eval_js", side_effect=_page_eval(_PAGE_CONTROLS)
        ),
        patch("tools.browser_vault_tool._eval_js_secret", side_effect=secret_eval),
        patch("tools.browser_vault_tool._ensure_supervisor", return_value=None),
        patch(
            "tools.approval_prompt.request_elicitation_consent", return_value="accept"
        ),
    ):
        moved = json.loads(
            fill_unbound_card(
                handle="op:vault:card",
                label="N26 Sera Credit Card",
                backend=backend,
                task_id="other-task",
                currency="EUR",
                amount="94.26",
                recipient="owner@example.com",
                items=_ITEMS,
                submit=False,
            )
        )
    assert moved["error_type"] == "payment_stale"


def test_stripe_frame_is_filled_in_its_session_and_other_frames_are_not():
    backend = _Backend()
    frame = SimpleNamespace(
        is_oopif=True, cdp_session_id="sid-1", url=_STRIPE, frame_id="frame-1"
    )
    supervisor = SimpleNamespace(
        _state_lock=threading.Lock(), _frames={"frame-1": frame}
    )

    def evaluate(expression, frame_session=None):
        assert frame_session == "sid-1"
        return {"ok": True, "result": json.dumps(_PAGE_CONTROLS)}

    supervisor.evaluate_runtime = evaluate
    secret_calls = []

    def secret_eval(task_id, expression, frame_session=None):
        secret_calls.append((frame_session, expression))
        return {"success": True, "result": json.dumps({"filled": 3})}

    def eval_js(task_id, expression):
        if "data-hermes-vault-slot" in expression:
            return {"success": True, "result": json.dumps([])}
        if "iframe" in expression:
            return {"success": True, "result": json.dumps([_STRIPE + "#fragment"])}
        return {"success": False, "error": "unexpected"}

    raw, body, _asks = _fill(
        backend, eval_js=eval_js, secret_eval=secret_eval, supervisor=supervisor
    )
    assert body["success"] is True and body["provider"] == "stripe"
    assert secret_calls[0][0] == "sid-1"
    assert STRIPE_FRAME_ORIGIN in secret_calls[0][1] and _PAN in secret_calls[0][1]
    assert _PAN not in raw

    def adyen_eval(task_id, expression):
        if "data-hermes-vault-slot" in expression:
            return {"success": True, "result": json.dumps([])}
        if "iframe" in expression:
            return {
                "success": True,
                "result": json.dumps([
                    "https://checkout.adyen.com/pay?session=SECRETTOKEN"
                ]),
            }
        return {"success": False, "error": "unexpected"}

    adyen = _Backend()
    raw_adyen, blocked, _asks = _fill(
        adyen, eval_js=adyen_eval, secret_eval=secret_eval, supervisor=None
    )
    assert blocked["error_type"] == "unsupported_payment_frame"
    assert blocked["frame_origins"] == ["https://checkout.adyen.com"]
    assert "SECRETTOKEN" not in raw_adyen
    assert adyen.resolves == 0


def test_unready_stripe_frame_fetches_nothing():
    backend = _Backend()

    def eval_js(task_id, expression):
        if "data-hermes-vault-slot" in expression:
            return {"success": True, "result": json.dumps([])}
        if "iframe" in expression:
            return {"success": True, "result": json.dumps([_STRIPE])}
        return {"success": False, "error": "unexpected"}

    _raw, body, _asks = _fill(
        backend,
        eval_js=eval_js,
        secret_eval=lambda *a, **k: pytest.fail("wrote a secret"),
        supervisor=None,
    )
    assert body["error_type"] == "payment_frame_unready"
    assert backend.resolves == 0


def test_submit_once_then_replay_does_not_click_again():
    backend = _Backend()
    clicks = []

    def eval_js(task_id, expression):
        if "ambiguous_submit" in expression:
            clicks.append(expression)
            return {"success": True, "result": json.dumps({"clicked": True})}
        return _page_eval(_PAGE_CONTROLS)(task_id, expression)

    def secret_eval(task_id, expression, frame_session=None):
        return {"success": True, "result": json.dumps({"filled": 3})}

    _raw, body, _asks = _fill(
        backend, eval_js=eval_js, secret_eval=secret_eval, submit=True
    )
    assert body["outcome"] == "ambiguous" and body["submit_consumed"] is True
    assert body["merchant_acceptance"] == "unverified"
    assert len(clicks) == 1
    _raw2, replay, _asks2 = _fill(
        backend, eval_js=eval_js, secret_eval=secret_eval, submit=True
    )
    assert replay["error_type"] == "payment_replay"
    assert len(clicks) == 1
    assert _PAN not in _raw2


def test_takeover_during_confirmation_writes_nothing():
    from tools.bot_desktop.lease import AGENT, Lease

    backend = _Backend()
    state = {"epoch": 0}

    def get_lease():
        return Lease(holder=AGENT, epoch=state["epoch"])

    def secret_eval(*_args, **_kwargs):
        raise AssertionError("secret eval")

    def consent(message, description, **_kwargs):
        state["epoch"] = 1
        return "accept"

    with (
        patch("tools.browser_vault_tool._focus_bound_origin", return_value=None),
        patch(
            "tools.browser_vault_tool._current_page_origin",
            return_value="https://tickets.mahalla.berlin",
        ),
        patch(
            "tools.browser_vault_tool._eval_js", side_effect=_page_eval(_PAGE_CONTROLS)
        ),
        patch("tools.browser_vault_tool._eval_js_secret", side_effect=secret_eval),
        patch("tools.browser_vault_tool._ensure_supervisor", return_value=None),
        patch("tools.bot_desktop.lease.get", side_effect=get_lease),
        patch("tools.bot_desktop.lease.assert_agent_may_act", side_effect=get_lease),
        patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent),
    ):
        stale = json.loads(
            fill_unbound_card(
                handle="op:vault:card",
                label="N26 Sera Credit Card",
                backend=backend,
                task_id="task",
                currency="EUR",
                amount="94.26",
                recipient="owner@example.com",
                items=_ITEMS,
                submit=False,
            )
        )
    assert stale["error_type"] == "payment_stale"
    assert backend.resolves == 0


def test_browser_vault_fill_dispatches_unbound_cards():
    backend = _Backend()
    backend.needs_unlock = False
    secret_calls = []

    def secret_eval(task_id, expression, frame_session=None):
        secret_calls.append(expression)
        return {"success": True, "result": json.dumps({"filled": 3})}

    with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
         patch("tools.browser_vault_tool._focus_bound_origin", return_value=None), \
         patch("tools.browser_vault_tool._current_page_origin", return_value="https://tickets.mahalla.berlin"), \
         patch("tools.browser_vault_tool._eval_js", side_effect=_page_eval(_PAGE_CONTROLS)), \
         patch("tools.browser_vault_tool._eval_js_secret", side_effect=secret_eval), \
         patch("tools.browser_vault_tool._ensure_supervisor", return_value=None), \
         patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
        from tools.browser_vault_tool import browser_vault_fill

        body = json.loads(browser_vault_fill(
            "op:vault:card", task_id="task", currency="EUR", amount="94.26",
            recipient="owner@example.com", items=_ITEMS,
        ))
    assert body["success"] is True and body["kind"] == "payment"
    assert secret_calls and _PAN in secret_calls[0]


def test_unknown_frame_session_never_reaches_cdp():
    supervisor = CDPSupervisor("task", "http://127.0.0.1:9")
    refused = supervisor.evaluate_runtime(
        "document.cookie", frame_session="not-a-session"
    )
    assert refused["ok"] is False
    assert refused["error"] == "unknown frame session"


def test_frame_query_must_match_or_the_frame_stays_unready():
    src = _STRIPE
    frames = [{"frame_id": "f", "session_id": "sid", "url": src + "&extra=1"}]
    classified = classify_embedded_frames(
        [src], frames, merchant_origin="https://shop.test"
    )
    assert classified["supported"] == []
    assert classified["unready"] == [STRIPE_FRAME_ORIGIN]
    matched = classify_embedded_frames(
        [src + "#x"],
        [{"frame_id": "f", "session_id": "sid", "url": src}],
        merchant_origin="https://shop.test",
    )
    assert matched["supported"][0]["session_id"] == "sid"
