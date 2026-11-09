"""Explicit signup intent, synthetic controls and mocked native core only."""
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.vault_login_classifier import (
    LoginControl, SignupTargets, build_signup_fill_js, classify_login_control, select_signup_targets,
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


def confirm(**changes):
    """An explicit confirmation field: the role comes from id/placeholder only, as on real sign-up forms."""
    return field(**dict({"index": 2, "name": " confirm-password-signup", "label": " Confirm password"}, **changes))


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
    # A confirmation pair is admitted only in its one explicit shape (see PAIRS below).
    [field(label="Confirm password"), confirm()], [field(), confirm(formIndex=1)],
    [field(), confirm(formAction=ORIGIN + "/register/other")], [field(index=3), confirm()],
    [field(), confirm(), field(index=3, name="pw3")],
    [field(), confirm(), field(index=3, autocomplete="current-password")],
    [field(), confirm(autocomplete="")], [field(), confirm(type="text")],
    [field(label="Current password"), confirm()], [field(), confirm(name="change-password-confirm")],
    [field(pageUrl=ORIGIN + "/reset-password"), confirm(pageUrl=ORIGIN + "/reset-password")],
    [field(), confirm(), field(index=3, autocomplete="one-time-code", type="text")],
    [field(), confirm(), field(index=3, autocomplete="email", type="email", formIndex=1)],
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
    assert select_signup_targets([control]) == SignupTargets(control)
    script = build_signup_fill_js([{"index": 1, "token": "new-password", "value": SECRET}], ORIGIN, "nonce",
                                  SignupTargets(control))
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


# ── Paired signup: new password + ONE explicit confirmation field ─────────────────────────────────────────

PAIR = [field(), confirm()]


@pytest.mark.parametrize("confirmation", [
    confirm(), confirm(name="passwordRepeat", label=""), confirm(name="password_confirmation", label=""),
    confirm(name="", label="Re-enter password"), confirm(name="", label="Verify password"),
    confirm(name="", label="Enter the password again"),
])
def test_explicit_confirmation_pairs_with_the_new_password(confirmation):
    targets = select_signup_targets([LoginControl.from_dict(c) for c in (field(), confirmation)])
    assert targets is not None and (targets.primary.index, targets.confirm.index) == (1, 2)
    assert select_signup_targets([LoginControl.from_dict(field())]).confirm is None  # one field: unchanged


def test_pair_consent_names_both_fields_then_one_resolution_and_one_write(core):
    core.inspect.return_value["result"] = json.dumps(PAIR)
    core.write.side_effect = lambda *a: core.events.append("write") or {
        "success": True, "result": json.dumps({"filled": 2, "retained": [True, True]})}
    result = fill()
    assert result["success"] and result["filled_fields"] == 2 and result["retention_verified"]
    assert core.events == ["consent", "resolve", "redact", "write"]
    description = core.consent.call_args.args[1]
    assert "TWO fields" in description and "confirm-password" in description and "Nothing will be submitted" in description
    assert SECRET not in json.dumps(result)


def test_single_field_consent_does_not_claim_two_fields(core):
    fill()
    assert "exactly one new-password field" in core.consent.call_args.args[1]
    assert "TWO" not in core.consent.call_args.args[1]


def test_declined_pair_resolves_nothing(core):
    core.inspect.return_value["result"] = json.dumps(PAIR)
    core.consent.side_effect = None
    core.consent.return_value = "decline"
    assert fill()["error_type"] == "signup_declined"
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


@pytest.mark.parametrize("answer,error_type", [
    ({"success": True, "result": json.dumps({"partial": "signup_target_changed", "attempted": 1})}, "signup_fill_partial"),
    ({"success": True, "result": json.dumps({"partial": "signup_write_failed", "attempted": 2})}, "signup_fill_partial"),
    ({"success": True, "result": json.dumps({"filled": 2, "retained": [True, False]})}, "signup_fields_not_retained"),
    ({"success": True, "result": json.dumps({"filled": 2, "retained": [False, True]})}, "signup_fields_not_retained"),
    ({"success": True, "result": json.dumps({"filled": 1})}, "signup_fill_uncertain"),
    ({"success": True, "result": "[object Promise]"}, "signup_fill_uncertain"),
    ({"success": False, "error_type": "eval_failed", "error": SECRET}, "signup_fill_uncertain"),
    (RuntimeError(SECRET), "signup_fill_uncertain"),
])
def test_pair_without_both_fields_retained_is_typed_not_success(core, answer, error_type):
    core.inspect.return_value["result"] = json.dumps(PAIR)
    core.write.side_effect = answer if isinstance(answer, Exception) else None
    core.write.return_value = answer
    result = fill()
    assert result["success"] is False and result["error_type"] == error_type and result["partially_filled"]
    assert "Nothing was written" not in result["error"] and "do not retry" in result["error"]
    assert result.get("filled_fields", 0) < 2 or error_type == "signup_fields_not_retained"
    assert SECRET not in json.dumps(result)
    assert core.write.call_count == 1  # no hidden retry


_PAIR_DOM = r'''
const mutation = @@MUTATION@@;
const expected = @@EXPECTED@@;
const origin = @@ORIGIN@@;
const url = origin + '/register';
const window = { location: { origin, href: url } };
const writes = [];
class Event { constructor(type) { this.type = type; } }
class InputEvent extends Event {}
class HTMLInputElement {
  constructor(id, placeholder, form, slot) {
    Object.assign(this, { tagName: 'INPUT', type: 'password', autocomplete: 'new-password', name: '', id, form,
      disabled: false, readOnly: false, labels: [], hidden: false, connected: true, held: '', listeners: {},
      attrs: { placeholder, 'data-hermes-vault-slot': slot } });
  }
  get value() { return this.held; }
  set value(v) { if (this.throws) throw new Error('setter'); writes.push(this.id); this.held = v; }
  get isConnected() { return this.connected; }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  hasAttribute(n) { return n in this.attrs; }
  removeAttribute(n) { delete this.attrs[n]; }
  getClientRects() { return this.hidden ? [] : [1]; }
  getRootNode() { return document; }
  focus() {}
  on(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatchEvent(ev) { for (const f of this.listeners[ev.type] || []) f(ev); return true; }
}
const form = { action: url, elements: [] };
const p = new HTMLInputElement('password-signup', 'Password', form, 'nonce:1');
const c = new HTMLInputElement('confirm-password-signup', 'Confirm password', form, 'nonce:2');
let inputs = [p, c];
const getComputedStyle = () => ({ display: 'block', visibility: 'visible' });
const document = {
  querySelectorAll: (s) => {
    if (s === 'input') return inputs;
    const m = /^\[data-hermes-vault-slot(?:="(.*)")?\]$/.exec(s);
    return inputs.filter((e) => m[1] === undefined ? 'data-hermes-vault-slot' in e.attrs : e.attrs['data-hermes-vault-slot'] === m[1]);
  },
  getElementById: () => null,
};
if (mutation === 'origin') window.location.origin = 'https://wrong.example.test';
if (mutation === 'url') window.location.href = url + '/reset';
if (mutation === 'confirm_readonly') c.readOnly = true;
if (mutation === 'confirm_disabled') c.disabled = true;
if (mutation === 'confirm_hidden') c.hidden = true;
if (mutation === 'third_password') inputs.push(new HTMLInputElement('pw3', 'Password', form, 'nonce:3'));
if (mutation === 'otp') inputs.push(Object.assign(new HTMLInputElement('otp', 'Code', form, 'nonce:3'), { type: 'text', autocomplete: 'one-time-code' }));
if (mutation === 'roles_swapped') { c.id = 'password2'; c.attrs.placeholder = 'Password'; p.attrs.placeholder = 'Confirm password'; }
if (mutation === 'other_form') c.form = { action: url, elements: [] };
if (mutation === 'formaction') form.elements.push({ hasAttribute: (a) => a === 'formaction' });
if (mutation === 'duplicate_stamp') inputs.push(new HTMLInputElement('copy', 'Confirm password', form, 'nonce:2'));
if (mutation === 'relabel_on_input') p.on('input', () => { c.id = 'password2'; c.attrs.placeholder = 'Password'; });
if (mutation === 'substitute_on_input') p.on('input', () => {
  c.connected = false;
  inputs = [p, new HTMLInputElement('confirm-password-signup', 'Confirm password', form, 'nonce:2')];
});
if (mutation === 'confirm_cleared') c.on('change', () => setTimeout(() => { c.held = ''; }, 5));
if (mutation === 'primary_cleared') c.on('input', () => { p.held = ''; });
if (mutation === 'setter_throws') c.throws = true;
Promise.resolve(@@SCRIPT@@).then((raw) => {
  process.stdout.write(JSON.stringify({ result: JSON.parse(raw), writes, held: [p.held === expected, c.held === expected],
    stamped: inputs.some((e) => 'data-hermes-vault-slot' in e.attrs) }));
});
'''


def _run_pair_js(script, mutation="none", expected=SECRET):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for synthetic JS probe")
    import re
    values = {"@@MUTATION@@": json.dumps(mutation), "@@ORIGIN@@": json.dumps(ORIGIN),
              "@@EXPECTED@@": json.dumps(expected), "@@SCRIPT@@": script}
    harness = re.sub("|".join(values), lambda m: values[m.group(0)], _PAIR_DOM)  # one pass: never rescans values
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=20, check=False)
    assert run.returncode == 0, run.stderr.replace(expected, "[redacted]")
    assert expected not in run.stdout and json.dumps(expected) not in run.stdout
    return json.loads(run.stdout)


def _pair_script(value=SECRET):
    targets = select_signup_targets([LoginControl.from_dict(c) for c in PAIR])
    fills = [{"index": 1, "token": "new-password", "value": value}, {"index": 2, "token": "confirm-password", "value": value}]
    return build_signup_fill_js(fills, ORIGIN, "nonce", targets)


@pytest.mark.parametrize("mutation,refused", [
    ("origin", "origin_changed"), ("url", "signup_target_changed"), ("confirm_readonly", "signup_target_changed"),
    ("confirm_disabled", "signup_target_changed"), ("confirm_hidden", "signup_target_changed"),
    ("third_password", "signup_target_changed"), ("otp", "signup_target_changed"),
    ("roles_swapped", "signup_target_changed"), ("other_form", "signup_target_changed"),
    ("formaction", "signup_target_changed"), ("duplicate_stamp", "signup_target_changed"),
])
def test_pair_script_revalidates_whole_set_before_first_write(monkeypatch, mutation, refused):
    monkeypatch.setattr("agent.vault_login_classifier.SIGNUP_SETTLE_MS", 20)
    observed = _run_pair_js(_pair_script(), mutation)
    assert observed["result"]["refused"] == refused and observed["writes"] == []


@pytest.mark.parametrize("mutation,expected", [
    ("relabel_on_input", {"partial": "signup_target_changed", "attempted": 1}),
    ("substitute_on_input", {"partial": "signup_target_changed", "attempted": 1}),
    ("setter_throws", {"partial": "signup_write_failed", "attempted": 2}),
    ("confirm_cleared", {"filled": 2, "retained": [True, False]}),
    ("primary_cleared", {"filled": 2, "retained": [False, True]}),
])
def test_pair_script_reports_partial_and_lost_fields(monkeypatch, mutation, expected):
    monkeypatch.setattr("agent.vault_login_classifier.SIGNUP_SETTLE_MS", 20)
    observed = _run_pair_js(_pair_script(), mutation)
    assert observed["result"] == expected
    # A changed or substituted confirmation field is never written; retention failures wrote both.
    assert observed["writes"] == ["password-signup"] + ([] if "partial" in expected else ["confirm-password-signup"])
    assert not observed["stamped"]


@pytest.mark.parametrize("value", [
    'q"uo\'te`s', "back\\slash\\\\", "ünï©ødé 密码   ", "new\nline\r\ttab",
    "__VALUE__ __BOUND__ __ORIGIN__ __SETTLE_MS__ __CONFIRM_RE__ ${x} </script>",
])
def test_pair_through_the_tool_round_trips_tricky_passwords_and_never_returns_them(core, monkeypatch, value):
    """Real gate, consent, redaction and generated JS end to end; the page is a synthetic DOM in Node."""
    monkeypatch.setattr("agent.vault_login_classifier.SIGNUP_SETTLE_MS", 20)
    monkeypatch.setattr(vault.secrets, "token_hex", lambda n: "nonce")
    core.inspect.return_value["result"] = json.dumps(PAIR)
    core.backend.resolve_password.side_effect = lambda h: core.events.append("resolve") or value
    seen = {}

    def write(task_id, script):
        seen.update(_run_pair_js(script, expected=value))
        return {"success": True, "result": json.dumps(seen["result"])}

    core.write.side_effect = write
    result = fill()
    assert result["success"] and result["filled_fields"] == 2, result
    assert seen["held"] == [True, True] and seen["writes"] == ["password-signup", "confirm-password-signup"]
    assert value not in json.dumps(result) and json.dumps(value) not in json.dumps(result)
    core.redaction.assert_called_once_with(value)
