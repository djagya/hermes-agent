"""Explicit signup intent, synthetic controls and mocked native core only."""
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.vault_login_classifier import (
    LoginControl, build_signup_fill_js, classify_login_control, select_signup_control,
)
from tools import browser_vault_tool as vault
from tools.approval_prompt import request_elicitation_consent as REAL_CONSENT

ORIGIN = "https://app.dataforseo.com"
SECRET = "synthetic-signup-password-__FILL__-__BOUND__"
HANDLE = "synthetic-login"


def field(**changes):
    return dict({"autocomplete": "new-password", "formIndex": 0, "index": 1,
                 "name": "password password", "label": "Password", "type": "password",
                 "pageUrl": ORIGIN + "/register", "formAction": ORIGIN + "/register"}, **changes)


@pytest.fixture
def core(monkeypatch):
    events = []
    meta = SimpleNamespace(kind="login", label="Synthetic DataForSEO", origin=ORIGIN,
                           allowed_origins=[ORIGIN], has_otp=False)
    backend = SimpleNamespace(name="synthetic", needs_unlock=False, get_meta=Mock(return_value=meta),
                              resolve_password=Mock(side_effect=lambda h: events.append("resolve") or SECRET))
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", Mock(return_value=backend))
    origin = Mock(return_value=ORIGIN)
    monkeypatch.setattr(vault, "_fill_page_origin", origin)
    monkeypatch.setattr(vault, "_current_page_origin", Mock(return_value=ORIGIN))
    inspect = Mock(return_value={"success": True, "result": json.dumps([field()])})
    monkeypatch.setattr(vault, "_eval_js", inspect)
    consent = Mock(side_effect=lambda *a, **kw: events.append("consent") or "accept")
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", consent)
    redaction = Mock(side_effect=lambda s: events.append("redact"))
    monkeypatch.setattr("agent.redact.register_vault_redaction_value", redaction)
    write = Mock(side_effect=lambda *a: events.append("write") or
                 {"success": True, "result": json.dumps({"filled": 1})})
    monkeypatch.setattr(vault, "_eval_js_secret", write)
    return SimpleNamespace(**locals())


def fill(**kw):
    return json.loads(vault.browser_vault_fill(HANDLE, purpose="signup", **kw))


def test_signup_explicit_native_consent_before_resolve_and_redaction_before_write(core):
    result = fill()
    assert result["success"] and result["purpose"] == "signup"
    assert core.events == ["consent", "resolve", "redact", "write"]
    message, description = core.consent.call_args.args
    assert HANDLE in message and ORIGIN in message and "Create account" in message
    assert "new-password" in description and "Nothing will be submitted" in description
    assert core.consent.call_args.kwargs["surface"] == "vault-signup"
    assert SECRET not in json.dumps(result)
    assert SECRET not in core.inspect.call_args.args[1]
    assert SECRET in core.write.call_args.args[1]
    assert "new-password" in core.write.call_args.args[1]


@pytest.mark.parametrize("choice", ["decline", "cancel", "timeout", None, "once", "session"])
def test_no_explicit_accept_means_zero_secret_resolution_or_writes(core, choice):
    core.consent.side_effect = None
    core.consent.return_value = choice
    assert fill()["error_type"] == "signup_declined"
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


def test_unavailable_confirmation_fails_closed(core):
    core.consent.side_effect = RuntimeError("unavailable")
    assert fill()["error_type"] == "signup_declined"
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


@pytest.mark.parametrize("gateway", [True, False])
def test_real_consent_with_nobody_to_answer_refuses_before_resolution(core, monkeypatch, gateway):
    # The real routing: a gateway session with no approval callback, or a cron/-q/unattended worker.
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", REAL_CONSENT)
    monkeypatch.setattr("tools.approval_context._is_gateway_approval_context", lambda: gateway)
    monkeypatch.setattr("tools.approval_context._get_session_platform", lambda: "cron")
    monkeypatch.setattr("tools.approval_context._no_user_can_answer", lambda: True)
    monkeypatch.setattr("tools.approval._gateway_notify_cb", lambda key: None)
    assert fill()["error_type"] == "signup_declined"
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


@pytest.mark.parametrize("purpose", ["login", "signup"])
def test_typed_backend_errors_keep_their_advice(core, purpose):
    from agent.vault_backends.base import BackendRateLimited

    core.backend.resolve_password.side_effect = BackendRateLimited("synthetic")
    if purpose == "login":
        core.inspect.return_value["result"] = json.dumps([field(autocomplete="current-password")])
    # Raised, not swallowed: _fenced_page_op turns a typed failure into its own result and advice.
    with pytest.raises(BackendRateLimited):
        vault.browser_vault_fill(HANDLE, purpose=purpose)
    core.write.assert_not_called()


def test_wrong_origin_before_consent_or_resolution(core):
    core.origin.return_value = "https://wrong.test"
    assert fill()["error_type"] == "origin_mismatch"
    core.consent.assert_not_called()
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


def test_changed_origin_during_confirmation(core, monkeypatch):
    monkeypatch.setattr(vault, "_current_page_origin", lambda tid: "https://wrong.test")
    assert fill()["error_type"] == "origin_changed"
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


BAD_CONTROLS = [
    [], [field(), field(index=2)],
    [field(), field(index=2, autocomplete="current-password")],
    [field(), field(index=2, name="confirmation", label="Confirm password")],
    [field(), field(index=2, autocomplete="one-time-code", type="text")],
    [field(), field(index=2, autocomplete="", name="otp", type="text")],
    [field(), field(index=2, autocomplete="email", type="email", formIndex=1)],
    [field(autocomplete="current-password")], [field(type="text")], [field(formIndex=None)],
    [field(label="Confirm password")], [field(pageUrl=ORIGIN + "/reset-password")],
    [field(pageUrl=ORIGIN + "/account-recovery")],
    [field(pageUrl=ORIGIN + "/change-password")],
    [field(pageUrl=ORIGIN + "/forgot-password")],
    [field(formAction=ORIGIN + "/reset-password")],
    [field(formAction="https://other.test/register")],
    [field(pageUrl=ORIGIN + "/login", formAction=ORIGIN + "/login")],
    [field(pageUrl=ORIGIN + "/login?next=/signup")],
    [field(pageUrl=ORIGIN + "/login/after-signup", formAction=ORIGIN + "/login")],
    [field(pageUrl=ORIGIN + "/signup/resetpassword", formAction=ORIGIN + "/signup/resetpassword")],
    [field(pageUrl=ORIGIN + "/register/changepassword", formAction=ORIGIN + "/register/changepassword")],
    [field(name="password_confirmation")], [field(label="Retype password")],
    [field(pageUrl="http://app.dataforseo.com/register", formAction="http://app.dataforseo.com/register")],
]


@pytest.mark.parametrize("controls", BAD_CONTROLS)
def test_unsupported_controls_never_resolve_or_write(core, controls):
    core.inspect.return_value["result"] = json.dumps(controls)
    assert fill()["error_type"] == "signup_target_unsupported"
    core.consent.assert_not_called()
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


@pytest.mark.parametrize("kind", ["payment", "address", "unknown"])
def test_non_login_items_rejected(core, kind):
    core.meta.kind = kind
    assert fill()["error_type"] == "signup_login_required"
    core.consent.assert_not_called()
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


@pytest.mark.parametrize("purpose", ["reset", "", None, 1, ["signup"]])
def test_unknown_purpose_rejected(core, purpose):
    assert json.loads(vault.browser_vault_fill(HANDLE, purpose=purpose))["error_type"] == "invalid_purpose"
    core.backend.get_meta.assert_not_called()
    core.write.assert_not_called()


def test_login_default_still_excludes_new_password(core):
    assert classify_login_control(LoginControl.from_dict(field())) is None
    assert not json.loads(vault.browser_vault_fill(HANDLE))["success"]
    core.consent.assert_not_called()
    core.write.assert_not_called()


def test_login_default_still_fills_current_password_without_signup_consent(core):
    core.inspect.return_value["result"] = json.dumps([field(autocomplete="current-password")])
    assert json.loads(vault.browser_vault_fill(HANDLE))["success"]
    core.consent.assert_not_called()
    assert core.events == ["resolve", "redact", "write"]


@pytest.mark.parametrize("stage", ["resolve", "redact", "write", "write_result"])
def test_errors_never_return_secret(core, stage):
    if stage == "resolve":
        core.backend.resolve_password.side_effect = RuntimeError(SECRET)
    elif stage == "redact":
        core.redaction.side_effect = RuntimeError(SECRET)
    elif stage == "write":
        core.write.side_effect = RuntimeError(SECRET)
    else:
        core.write.side_effect = None
        core.write.return_value = {"success": False, "error": SECRET}
    result = fill()
    assert not result["success"] and SECRET not in json.dumps(result)
    if stage in ("resolve", "redact"):
        core.write.assert_not_called()


@pytest.mark.parametrize("refusal", ["origin_changed", "signup_target_changed"])
def test_native_refusal_is_not_success(core, refusal):
    core.write.side_effect = None
    core.write.return_value = {"success": True, "result": json.dumps({"refused": refusal, "found": SECRET})}
    result = fill()
    assert result["error_type"] == refusal
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("purpose", ["signup", "login", None])
def test_schema_and_handler_preserve_purpose_and_fence(monkeypatch, purpose):
    spec = vault.BROWSER_VAULT_SCHEMA["parameters"]["properties"]["purpose"]
    assert spec["enum"] == ["login", "signup"] and spec["default"] == "login"
    fence = Mock(side_effect=lambda tid, fn: fn())
    target = Mock(return_value='{"success": false}')
    monkeypatch.setattr(vault, "_fenced_page_op", fence)
    monkeypatch.setattr(vault, "browser_vault_fill", target)
    args = {"action": "fill", "handle": HANDLE}
    if purpose is not None:
        args["purpose"] = purpose
    vault._handle_vault(args, task_id="synthetic-task")
    fence.assert_called_once()
    assert target.call_args.kwargs == {"handle": HANDLE, "task_id": "synthetic-task",
                                      "merchant_origin": "", "purpose": purpose or "login"}


def test_signup_handler_cannot_bypass_human_ownership_fence(monkeypatch):
    target = Mock()
    monkeypatch.setattr(vault, "browser_vault_fill", target)
    monkeypatch.setattr(vault, "_fenced_page_op", lambda tid, fn: '{"success": false, "error_type": "human_owned"}')
    result = json.loads(vault._handle_vault({"action": "fill", "handle": HANDLE, "purpose": "signup"}))
    assert result["error_type"] == "human_owned"
    target.assert_not_called()


@pytest.mark.parametrize("mutation", ["none", "origin", "url", "type", "autocomplete", "action", "duplicate", "mixed", "otp", "hidden", "stamp", "formaction"])
def test_signup_script_against_synthetic_dom(mutation):
    """Exercise actual generated JS, not a fabricated browser response. No browser/vault."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for synthetic JS probe")
    control = LoginControl.from_dict(field())
    assert select_signup_control([control]) is not None
    script = build_signup_fill_js([{"index": 1, "token": "new-password", "value": SECRET}], ORIGIN, "nonce", control)
    harness = r'''
const mutation = MUTATION;
let writes = 0;
const origin = ORIGIN;
const window = { location: { origin, href: origin + '/register' } };
class HTMLInputElement {
  constructor() { this.type = 'password'; this.autocomplete = 'new-password';
    this.form = { action: origin + '/register', elements: [] }; this.tagName = 'INPUT'; this.held = ''; }
  get value() { return this.held; }
  set value(v) { writes++; this.held = v; }
  getClientRects() { return mutation === 'hidden' ? [] : [1]; }
  focus() {} dispatchEvent() {} removeAttribute() {}
}
const el = new HTMLInputElement();
const inputs = [el];
let targets = [el];
if (mutation === 'origin') window.location.origin = 'https://wrong.test';
if (mutation === 'url') window.location.href += '/reset';
if (mutation === 'type') el.type = 'text';
if (mutation === 'autocomplete') el.autocomplete = 'current-password';
if (mutation === 'action') el.form.action += '/reset';
if (mutation === 'duplicate') targets.push(new HTMLInputElement());
if (mutation === 'stamp') targets = [];
if (mutation === 'formaction') el.form.elements.push({hasAttribute: a => a === 'formaction'});
if (mutation === 'mixed') inputs.push(new HTMLInputElement());
if (mutation === 'otp') inputs.push({type: 'text', autocomplete: 'one-time-code', getClientRects: () => [1]});
const getComputedStyle = () => ({display: 'block', visibility: 'visible'});
const document = {
  querySelectorAll: s => s === 'input' ? inputs : targets,
  querySelector: s => s.includes('nonce:1') ? targets[0] : null
};
const InputEvent = class {};
const Event = class {};
const result = JSON.parse(SCRIPT);
process.stdout.write(JSON.stringify({result, writes}));
'''
    # One-pass interpolation avoids interpreting synthetic password placeholders.
    import re
    values = {"MUTATION": json.dumps(mutation), "ORIGIN": json.dumps(ORIGIN), "SCRIPT": script}
    harness = re.sub(r"MUTATION|ORIGIN|SCRIPT", lambda m: values[m.group(0)], harness)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr.replace(SECRET, "[redacted]")
    observed = json.loads(run.stdout)
    assert observed["writes"] == (1 if mutation == "none" else 0)
    assert SECRET not in run.stdout
    assert observed["result"].get("filled", 0) == (1 if mutation == "none" else 0)
