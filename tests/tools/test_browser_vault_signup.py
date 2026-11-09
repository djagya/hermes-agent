"""Explicit signup intent, synthetic controls and mocked native core only."""
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.vault_login_classifier import (
    SIGNUP_OUTCOMES, LoginControl, SignupTargets, bind_key, build_fill_js, build_signup_fill_js, classify_login_control,
    select_signup_targets,
)
from tools import browser_vault_signup as signup
from tools import browser_vault_tool as vault
from tools.approval_prompt import request_elicitation_consent as REAL_CONSENT

ORIGIN = "https://app.dataforseo.com"
SECRET = "synthetic-signup-password-__FILL__-__BOUND__"
HANDLE = "synthetic-login"
TOKENS = {outcome: f"hv-test-{n:02d}" for n, outcome in enumerate(SIGNUP_OUTCOMES)}


def field(**changes):
    return dict({"autocomplete": "new-password", "formIndex": 0, "index": 1,
                 "name": "password password", "label": "Password", "type": "password",
                 "pageUrl": ORIGIN + "/register", "formAction": ORIGIN + "/register", "rendered": True}, **changes)


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
    monkeypatch.setattr(signup, "outcome_tokens", lambda: dict(TOKENS))
    # A signup script answers with one of its outcome tokens; the login script with its JSON count.
    write = Mock(side_effect=lambda task, script: events.append("write") or {"success": True, "result": _honest(script)})
    monkeypatch.setattr(vault, "_eval_js_secret", write)
    return SimpleNamespace(**locals())


def _honest(script):
    """What an unchanged page answers: every approved field held (signup), or one field filled (login)."""
    import re
    marks = re.search(r'"marks": \[([^\]]*)\]', script)
    if marks is None:
        return json.dumps({"filled": 1})
    return TOKENS["mask:" + "1" * len(marks.group(1).split(","))]


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
    [field(), confirm(formAction=ORIGIN + "/register/other")],
    [field(), confirm(), field(index=3, name="pw3")],
    [field(), confirm(), field(index=3, autocomplete="current-password")],
    [field(), confirm(autocomplete="")], [field(), confirm(type="text")],
    [field(label="Current password"), confirm()], [field(), confirm(name="change-password-confirm")],
    [field(pageUrl=ORIGIN + "/reset-password"), confirm(pageUrl=ORIGIN + "/reset-password")],
    [field(), confirm(), field(index=3, autocomplete="one-time-code", type="text")],
    [field(), confirm(), field(index=3, autocomplete="email", type="email", formIndex=1)],
    # change/old/update intent in any spelling; a target the inspection did not see rendered.
    [field(name="updatePassword"), confirm(name="confirmPassword", label="")], [field(name="oldpassword")],
    [field(name="changepassword")], [field(label="Existing password")], [field(), confirm(rendered=False)],
    [{k: v for k, v in field().items() if k != "rendered"}],
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


@pytest.mark.parametrize("answer", [
    json.dumps({"refused": "signup_target_changed", "filled": 0}), json.dumps({"refused": "origin_changed", "found": SECRET}),
    json.dumps({"filled": 1}), "hv-forged", [TOKENS["mask:1"]], None,
])
def test_page_forged_answers_are_never_trusted(core, answer):
    """Only this fill's own outcome token counts: a page that rewrote JSON/Promise built-ins cannot fake a
    "nothing written" refusal or a success, and no page string is echoed."""
    core.write.side_effect = None
    core.write.return_value = {"success": True, "result": answer}
    result = fill()
    assert result["error_type"] == "signup_fill_uncertain" and "Nothing was written" not in result["error"]
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


# ── Paired signup: new password + ONE explicit confirmation field ─────────────────────────────────────────

PAIR = [field(), confirm()]


@pytest.mark.parametrize("controls", [
    [field(), confirm()], [field(), confirm(name="passwordRepeat", label="")],
    [field(), confirm(name="password_confirmation", label="")], [field(), confirm(name="", label="Re-enter password")],
    [field(), confirm(name="", label="Verify password")], [field(), confirm(name="", label="Enter the password again")],
    [confirm(index=1), field(index=2)],  # either DOM order: the roles are explicit
])
def test_explicit_confirmation_pairs_with_the_new_password(controls):
    targets = select_signup_targets([LoginControl.from_dict(c) for c in controls])
    by_role = {c["name"] + c["label"]: c["index"] for c in controls}
    assert targets is not None and targets.primary.index == by_role["password passwordPassword"]
    assert targets.confirm.index == next(i for k, i in by_role.items() if k != "password passwordPassword")
    assert select_signup_targets([LoginControl.from_dict(field())]).confirm is None  # one field: unchanged


@pytest.mark.parametrize("name", ["passwordConfirm", "retypePassword", "confirmpassword", "password_again",
                                  "newPassword", "verifyPassword"])
def test_default_login_never_takes_a_confirmation_password(name):
    assert classify_login_control(LoginControl.from_dict(field(autocomplete="", name=name, label=""))) is None
    assert classify_login_control(LoginControl.from_dict(field(autocomplete="", name="password", label=""))) is not None


def test_pair_consent_names_both_fields_then_one_resolution_and_one_write(core):
    core.inspect.return_value["result"] = json.dumps(PAIR)
    result = fill()
    assert result["success"] and result["filled_fields"] == 2 and result["retention_verified"]
    assert result["fields"] == ["new-password", "confirm-password"]
    assert core.events == ["consent", "resolve", "redact", "write"]
    description = core.consent.call_args.args[1]
    assert "TWO fields" in description and "confirm-password" in description and "Nothing will be submitted" in description
    assert SECRET not in json.dumps(result)


def test_single_field_consent_does_not_claim_two_fields(core):
    assert fill()["success"]
    assert "exactly one new-password field" in core.consent.call_args.args[1]
    assert "TWO" not in core.consent.call_args.args[1]


def test_declined_pair_resolves_nothing(core):
    core.inspect.return_value["result"] = json.dumps(PAIR)
    core.consent.side_effect = None
    core.consent.return_value = "decline"
    assert fill()["error_type"] == "signup_declined"
    core.backend.resolve_password.assert_not_called()
    core.write.assert_not_called()


@pytest.mark.parametrize("answer,error_type,extra", [
    ({"success": True, "result": TOKENS["none"]}, "signup_target_changed", {}),
    ({"success": True, "result": TOKENS["origin"]}, "origin_changed", {}),
    ({"success": True, "result": TOKENS["partial:1"]}, "signup_fill_partial", {"fields_possibly_written": 1}),
    ({"success": True, "result": TOKENS["partial:2"]}, "signup_fill_partial", {"fields_possibly_written": 2}),
    ({"success": True, "result": TOKENS["changed"]}, "signup_form_changed", {}),
    ({"success": True, "result": TOKENS["mask:10"]}, "signup_fields_not_retained",
     {"filled_fields": 1, "field_retention": {"new-password": "held", "confirm-password": "lost"}}),
    ({"success": True, "result": TOKENS["mask:01"]}, "signup_fields_not_retained", {"filled_fields": 1}),
    ({"success": True, "result": TOKENS["mask:1"]}, "signup_fill_uncertain", {}),  # a one-field answer to a pair
    ({"success": True, "result": json.dumps({"filled": 2, "retained": [True, True]})}, "signup_fill_uncertain", {}),
    ({"success": False, "error_type": "eval_failed", "error": SECRET}, "signup_fill_uncertain", {}),
    (RuntimeError(SECRET), "signup_fill_uncertain", {}),
])
def test_pair_outcomes_are_typed_and_only_full_retention_succeeds(core, answer, error_type, extra):
    core.inspect.return_value["result"] = json.dumps(PAIR)
    core.write.side_effect = answer if isinstance(answer, Exception) else None
    core.write.return_value = answer
    result = fill()
    assert result["success"] is False and result["error_type"] == error_type
    assert all(result[k] == v for k, v in extra.items())
    # "Nothing was written" only for the two proven pre-write tokens; every other answer says do not retry.
    assert ("Nothing was written" in result["error"]) is (error_type in ("signup_target_changed", "origin_changed"))
    assert error_type in ("signup_target_changed", "origin_changed") or "do not retry" in result["error"]
    assert SECRET not in json.dumps(result)
    assert core.write.call_count == 1  # no hidden retry


# ── The real generated scripts against a synthetic DOM in Node ───────────────────────────────────────────

BIND = "bindsecret"
_DOM = r'''
const spec = @@SPEC@@;
const expected = @@EXPECTED@@;
const writes = [];
let focusEvents = 0;
const window = { location: { origin: spec.origin, href: spec.url }, innerWidth: 1280, innerHeight: 800, scrollY: 0 };
class Event { constructor(type) { this.type = type; } }
class InputEvent extends Event {}
const KEY = spec.key;
const mark = (node, v) => Object.defineProperty(node, KEY, { value: v, configurable: true });
class Form { constructor(action) { this.tagName = 'FORM'; this.action = action; this.elements = []; } }
class HTMLInputElement {
  constructor(c) {
    Object.assign(this, { tagName: c.type === 'select' ? 'SELECT' : 'INPUT', type: c.type, autocomplete: c.autocomplete || '',
      name: c.name || '', id: '', idx: c.index, disabled: false, readOnly: false, labels: [], connected: true, held: '',
      listeners: {}, form: null, shown: true, transparent: false, rect: { left: 10, top: 10 + 40 * c.index, width: 200, height: 30 },
      attrs: { placeholder: c.label || '', 'data-hermes-vault-slot': spec.nonce + ':' + c.index } });
  }
  get value() { return this.held; }
  set value(v) { if (this.throws) throw new Error('setter'); writes.push(this.idx); this.held = v; }
  get isConnected() { return this.connected; }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  hasAttribute(n) { return n in this.attrs; }
  removeAttribute(n) { delete this.attrs[n]; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getClientRects() { return this.shown ? [this.rect] : []; }
  getBoundingClientRect() { const r = this.rect; return { ...r, right: r.left + r.width, bottom: r.top + r.height }; }
  checkVisibility() { return !this.transparent; }
  getRootNode() { return this.root || document; }
  focus() { focusEvents += 1; (this.listeners.focus || []).forEach((f) => f()); }
  on(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatchEvent(ev) { for (const f of this.listeners[ev.type] || []) f(ev); return true; }
}
const forms = [];
const formFor = (c) => {
  if (c.formIndex === null || c.formIndex === undefined) return null;
  if (!forms[c.formIndex]) { forms[c.formIndex] = new Form(c.formAction); mark(forms[c.formIndex], 'f:' + c.formIndex); }
  return forms[c.formIndex];
};
const make = (c) => { const e = new HTMLInputElement(c); e.form = formFor(c); if (e.form) e.form.elements.push(e); return e; };
const clone = (e) => { const c = make({ index: e.idx, type: e.type, autocomplete: e.autocomplete, name: e.name,
  label: e.attrs.placeholder }); c.attrs = { ...e.attrs }; c.form = e.form; return c; };  // attributes (stamp
// included) copy exactly, JS identity marks do not
let light = [], shadow = [];
const byIdx = {};
for (const c of spec.controls) { const e = make(c); mark(e, 'e:' + c.index); byIdx[c.index] = e; (c.shadow ? shadow : light).push(e); }
const select = (list, s) => {
  if (s === '*') return list;
  const m = /^\[data-hermes-vault-slot(?:="(.*)")?\]$/.exec(s);
  return m ? list.filter((e) => e.attrs && (m[1] === undefined ? 'data-hermes-vault-slot' in e.attrs : e.attrs['data-hermes-vault-slot'] === m[1])) : [];
};
const host = { tagName: 'DIV', shadowRoot: { querySelectorAll: (s) => select(shadow, s), getElementById: () => null } };
const getComputedStyle = () => ({ display: 'block', visibility: 'visible' });
const document = {
  URL: spec.url,
  documentElement: { scrollHeight: 2000 },
  querySelectorAll: (s) => select([...forms.filter(Boolean), ...light, ...(shadow.length ? [host] : [])], s),
  getElementById: () => null,
};
const later = (f) => setTimeout(f, 5);
@@MUTATE@@
shadow.forEach((e) => { e.root = host.shadowRoot; });
Promise.resolve(@@SCRIPT@@).then((raw) => {
  process.stdout.write(JSON.stringify({ raw, writes, focusEvents,
    held: Object.fromEntries(Object.entries(byIdx).map(([k, e]) => [k, e.held === expected])),
    stamped: [...light, ...shadow].some((e) => 'data-hermes-vault-slot' in e.attrs) }));
});
'''


def _run(script, controls, mutate="", expected=SECRET, nonce="nonce"):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for synthetic JS probe")
    import re
    spec = {"controls": controls, "origin": ORIGIN, "url": ORIGIN + "/register", "nonce": nonce, "key": bind_key(BIND)}
    values = {"@@SPEC@@": json.dumps(spec), "@@EXPECTED@@": json.dumps(expected), "@@MUTATE@@": mutate, "@@SCRIPT@@": script}
    harness = re.sub("|".join(values), lambda m: values[m.group(0)], _DOM)  # one pass: never rescans values
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=20, check=False)
    assert run.returncode == 0, run.stderr.replace(expected, "[redacted]")
    assert expected not in run.stdout and json.dumps(expected)[1:-1] not in run.stdout
    observed = json.loads(run.stdout)
    raw = observed["raw"]
    observed["outcome"] = {t: o for o, t in TOKENS.items()}.get(raw, raw) if isinstance(raw, str) else raw
    return observed


def _signup_script(controls, value=SECRET, targets=None):
    targets = targets or select_signup_targets([LoginControl.from_dict(c) for c in controls])
    fills = [{"index": c.index, "token": "x", "value": value} for c in targets.controls]
    return build_signup_fill_js(fills, ORIGIN, "nonce", targets, bind=BIND, tokens=TOKENS)


@pytest.fixture
def quick_settle(monkeypatch):
    monkeypatch.setattr("agent.vault_login_classifier.SIGNUP_SETTLE_MS", 20)


ADD = "light.push(make({index: 3, formIndex: 0, formAction: spec.url, %s}));"


@pytest.mark.parametrize("mutate,outcome", [
    ("", "mask:11"),
    ("window.location.origin = 'https://wrong.example.test';", "origin"),
    ("window.location.href += '/reset';", "none"),
    ("byIdx[2].readOnly = true;", "none"), ("byIdx[2].disabled = true;", "none"), ("byIdx[2].shown = false;", "none"),
    ("byIdx[2].rect.width = 0;", "none"), ("byIdx[2].rect.left = -9999;", "none"), ("byIdx[2].transparent = true;", "none"),
    (ADD % "type: 'password', autocomplete: 'new-password', name: 'pw3', label: 'Password'", "none"),
    ("shadow.push(make({index: 3, formIndex: 0, formAction: spec.url, type: 'password', name: 'pw3'}));", "none"),
    (ADD % "type: 'text', autocomplete: '', name: 'otp', label: ''", "none"),
    ("light.push(make({index: 3, formIndex: 1, formAction: spec.url, type: 'email', autocomplete: 'email', name: 'email'}));", "none"),
    ("byIdx[2].name = 'pw'; byIdx[2].attrs.placeholder = 'Password'; byIdx[1].attrs.placeholder = 'Confirm password';", "none"),
    ("const f = new Form(spec.url); byIdx[1].form = f; byIdx[2].form = f;", "none"),  # re-parented, same action
    ("light = light.map(clone);", "none"),  # clones carrying the unique stamps
    ("shadow.push(clone(byIdx[2]));", "none"),  # duplicate stamp inside a shadow root
    ("forms[0].elements.push({ hasAttribute: (a) => a === 'formaction' });", "none"),
    ("byIdx[2].on('focus', () => { byIdx[2].type = 'text'; });", "mask:11"),  # no focus(): handler never runs
    # First write's events (the only page code between writes): the second write never happens.
    ("byIdx[1].on('input', () => { byIdx[2].type = 'text'; });", "partial:1"),
    ("byIdx[1].on('input', () => { forms[0].action = 'https://evil.example.test/x'; });", "partial:1"),
    ("byIdx[1].on('input', () => { byIdx[2].attrs.placeholder = 'Password'; byIdx[2].name = 'pw'; });", "partial:1"),
    ("byIdx[1].on('input', () => { light = light.map((e) => e === byIdx[2] ? clone(e) : e); });", "partial:1"),
    ("byIdx[1].on('input', () => { %s });" % (ADD % "type: 'text', autocomplete: '', name: 'otp'"), "partial:1"),
    ("byIdx[2].throws = true;", "partial:2"),
    # After the last write and after the settle the whole set is checked again.
    ("byIdx[2].on('input', () => { byIdx[2].type = 'text'; });", "changed"),
    ("byIdx[2].on('change', () => { %s });" % (ADD % "type: 'password', name: 'pw3'"), "changed"),
    ("byIdx[2].on('change', () => later(() => { forms[0].action = 'https://evil.example.test/x'; }));", "changed"),
    # Retention.
    ("byIdx[2].on('change', () => later(() => { byIdx[2].held = ''; }));", "mask:10"),
    ("byIdx[2].on('input', () => { byIdx[1].held = ''; });", "mask:01"),
])
def test_pair_script_against_synthetic_dom(quick_settle, mutate, outcome):
    observed = _run(_signup_script(PAIR), PAIR, mutate)
    assert observed["outcome"] == outcome
    written = {"none": [], "origin": [], "partial:1": [1], "partial:2": [1]}.get(outcome, [1, 2])
    assert observed["writes"] == written  # a changed or substituted second field is never written
    assert observed["focusEvents"] == 0
    assert observed["stamped"] is (outcome == "origin")  # stamps are cleared unless the page already navigated


@pytest.mark.parametrize("controls", [
    [dict(c, shadow=True) for c in PAIR],  # a pair inside an open shadow root works end to end
    [confirm(index=1), field(index=2)],  # confirmation first in the markup
    [field()],  # one field
])
def test_supported_shapes_fill_end_to_end_in_the_page(quick_settle, controls):
    observed = _run(_signup_script(controls), controls)
    assert observed["outcome"] == "mask:" + "1" * len(controls)
    assert all(observed["held"].values())


@pytest.mark.parametrize("extra", [
    field(index=3, name="pw3"), field(index=3, autocomplete="current-password"),
    field(index=3, type="text", autocomplete="", name="otp", label=""),
    field(index=3, type="text", autocomplete="one-time-code", name="code", label=""),
    field(index=3, type="email", autocomplete="", name="email", label="", formIndex=1),
])
def test_page_census_refuses_what_the_classifier_refuses(quick_settle, extra):
    """Parity: a shape Python refuses is refused by the page gate on its own, before any write."""
    controls = PAIR + [extra]
    assert select_signup_targets([LoginControl.from_dict(c) for c in controls]) is None
    forced = SignupTargets(LoginControl.from_dict(field()), LoginControl.from_dict(confirm()))
    observed = _run(_signup_script(controls, targets=forced), controls)
    assert observed["outcome"] == "none" and observed["writes"] == []


@pytest.mark.parametrize("shadow", [False, True])
def test_real_inspection_then_real_writer_on_one_page(quick_settle, shadow):
    """The inspection's stamps, identity marks and rendered flag are what the writer checks: inspect, select in
    Python, then (same DOM, inspection re-run first) write."""
    from agent.vault_login_classifier import build_inspection_js

    controls = [dict(c, shadow=shadow) for c in PAIR]
    inspection = build_inspection_js("nonce", BIND)
    inspected = json.loads(_run(inspection, controls)["raw"])
    assert [c["rendered"] for c in inspected] == [True, True]
    targets = select_signup_targets([LoginControl.from_dict(c) for c in inspected])
    assert targets is not None and targets.confirm is not None
    observed = _run(_signup_script(None, targets=targets), controls, mutate=inspection + ";")
    assert observed["outcome"] == "mask:11" and all(observed["held"].values())
    clone = _run(_signup_script(None, targets=targets), controls,
                 mutate=inspection + "; light = light.map(clone); shadow = shadow.map(clone);")
    assert clone["outcome"] == "none" and clone["writes"] == []


def test_login_writer_rechecks_its_target_after_focus():
    controls = [field(autocomplete="current-password")]
    script = build_fill_js([{"index": 1, "token": "current-password", "value": SECRET}], ORIGIN, "nonce")
    observed = _run(script, controls, "byIdx[1].on('focus', () => { byIdx[1].type = 'text'; });")
    assert observed["writes"] == [] and json.loads(observed["raw"]) == {"filled": 0}


@pytest.mark.parametrize("value", [
    'q"uo\'te`s', "back\\slash\\\\", "ünï©ødé 密码   ", "new\nline\r\ttab",
    "__VALUE__ __BOUND__ __TOKENS__ __ORIGIN__ __SETTLE_MS__ __PATTERNS__ ${x} </script>",
])
def test_pair_through_the_tool_round_trips_tricky_passwords_and_never_returns_them(core, monkeypatch, quick_settle, value):
    """Real gate, consent, redaction, generated JS and the secret wrapper end to end; the page is a synthetic
    DOM in Node."""
    monkeypatch.setattr(vault, "secrets", SimpleNamespace(token_hex=lambda n: BIND))  # nonce/bind; tokens stay random
    monkeypatch.setattr(vault, "_inspect_fill_controls", lambda task, nonce, kind, bind: (PAIR, [], None))
    core.backend.resolve_password.side_effect = lambda h: core.events.append("resolve") or value
    seen = {}

    def write(task_id, script):
        seen.update(_run(vault._secret_wrapped(script), PAIR, expected=value, nonce=BIND))
        return {"success": True, "result": seen["raw"]}

    core.write.side_effect = write
    result = fill()
    assert result["success"] and result["filled_fields"] == 2, result
    assert all(seen["held"].values()) and seen["writes"] == [1, 2]
    assert value not in json.dumps(result) and json.dumps(value) not in json.dumps(result)
    core.redaction.assert_called_once_with(value)


def test_secret_wrapper_answers_an_async_rejection_in_page():
    """A page handler can make an awaited step reject with the value it read; the wrapper must catch it in-page
    so no backend (Camofox logs err.message) ever sees the message."""
    rejecting = "(async () => { await null; throw new Error(" + json.dumps(SECRET) + "); })()"
    observed = _run(vault._secret_wrapped(rejecting), [])
    assert observed["raw"] == vault._PAGE_FILL_FAILED


@pytest.mark.parametrize("route", ["page", "supervisor"])
def test_every_secret_transport_gets_the_wrapper(monkeypatch, route):
    seen = []
    failed = json.dumps(vault._PAGE_FILL_FAILED)
    page = SimpleNamespace(name="fakepage", evaluate=lambda task, expr, secret: seen.append(expr) or failed)
    sup = SimpleNamespace(evaluate_runtime=lambda expr: seen.append(expr) or {"ok": True, "result": failed})
    monkeypatch.setattr(vault, "_is_managed", lambda: False)
    monkeypatch.setattr(vault, "_page_backend", lambda task: page if route == "page" else None)
    monkeypatch.setattr(vault, "_ensure_supervisor", lambda task: sup)
    monkeypatch.setattr(vault, "_bot_desktop_browser_session", lambda task: False)
    out = vault._eval_js_secret("t", "fill()")
    assert seen == [vault._secret_wrapped("fill()")] and seen[0].startswith("(async () => { try { return await (")
    assert out["success"] is False and out["error_type"] == "eval_failed"


def test_login_origin_refusal_needs_its_tag_and_never_echoes_the_page(core):
    import re
    core.inspect.return_value["result"] = json.dumps([field(autocomplete="current-password")])
    core.write.side_effect = lambda task, script: {"success": True, "result": json.dumps(
        {"refused": "origin_changed", "tag": re.search(r'tag: "(hv-[0-9a-f]+)"', script).group(1), "found": SECRET})}
    real = json.loads(vault.browser_vault_fill(HANDLE))
    assert real["error_type"] == "origin_changed" and "Nothing was written" in real["error"]
    core.write.side_effect = lambda task, script: {"success": True, "result": json.dumps(
        {"refused": "origin_changed", "found": "https://" + SECRET + ".test"})}
    forged = json.loads(vault.browser_vault_fill(HANDLE))
    assert forged["error_type"] == "fill_result_unverified" and "Nothing was written" not in forged["error"]
    assert SECRET not in json.dumps(real) + json.dumps(forged)
