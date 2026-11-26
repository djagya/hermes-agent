"""browser_vault reset_password / enroll_totp (J-0643, D-1085 a, D-1086 a): save first, then write the page; a
failed save writes nothing, a failed write after the save says the item was saved; the password and the key
never reach a result, a log line or an error; sensitive (bank-like) slots are refused. Fake backend and page."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.vault_backends.base import BackendError, BackendNetwork
from agent.vault_backends.onepassword import OnePasswordLoginBackend, TotpAlreadySet, clear_list_cache
from agent.vault_login_classifier import SIGNUP_OUTCOMES
from tests.agent import fake_op
from tools import browser_vault_account as account
from tools import browser_vault_signup as signup
from tools import browser_vault_tool as vault
from tools.registry import registry

ORIGIN = "https://shop.example"
HANDLE = "op:vault-h:abc"
TOKENS = {outcome: f"hv-test-{n:02d}" for n, outcome in enumerate(SIGNUP_OUTCOMES)}
KEY = "JBSWY3DPEHPK3PXP"
KEY_URI = f"otpauth://totp/Shop:jane?secret={KEY}&issuer=Shop"
GENERATED = "Gen-pw_9f3kQ7zL-20x!"  # what the fake 1Password generated and reads back


def new_password_field(**changes):
    return dict({"autocomplete": "new-password", "formIndex": 0, "index": 1, "name": "password", "label": "New password",
                 "type": "password", "pageUrl": ORIGIN + "/reset-password?token=t", "rendered": True,
                 "formAction": ORIGIN + "/reset-password",
                 "formText": "Reset your password New password Confirm password Save"}, **changes)


CONFIRM = new_password_field(index=2, name="confirm-password", label="Confirm password")
CODE_FIELD = {"autocomplete": "one-time-code", "formIndex": 0, "index": 3, "name": "otp", "label": "Code",
              "type": "text", "pageUrl": ORIGIN + "/settings/2fa", "rendered": True, "visible": True}


@pytest.fixture
def page(monkeypatch):
    """A fake 1Password backend and page. ``page.controls`` is what the inspection sees, ``page.keys`` what the
    key reader finds; ``page.events`` records save / write order."""
    events, written = [], []
    meta = SimpleNamespace(id=HANDLE, kind="login", label="Shop", origin=ORIGIN, allowed_origins=[ORIGIN],
                           has_otp=False)
    backend = SimpleNamespace(
        name="onepassword", display_name="1Password", needs_unlock=False, get_meta=Mock(return_value=meta),
        generate_login=Mock(side_effect=lambda handle, **kw: events.append("save") or (handle or "op:vault-h:new")),
        resolve_password=Mock(return_value=GENERATED),
        add_totp=Mock(side_effect=lambda handle, key: events.append("save") or True),
        resolve_otp=Mock(side_effect=lambda handle: events.append("mint") or "123456"))
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", Mock(return_value=backend))
    monkeypatch.setattr("agent.vault_backends.enabled_backends", Mock(return_value=[backend]))
    monkeypatch.setattr("tools.browser_use_cli.vault_route", Mock(return_value={"slot_id": "shopping"}))
    monkeypatch.setattr(vault, "_fill_page_origin", Mock(return_value=ORIGIN))
    monkeypatch.setattr(vault, "_focus_bound_origin", Mock(return_value=None))
    monkeypatch.setattr(vault, "_current_page_origin", Mock(return_value=ORIGIN))
    account._PENDING.clear()
    state = SimpleNamespace(controls=[new_password_field(), CONFIRM], keys={"uris": [KEY_URI], "keys": [KEY]})

    def read(task, script):
        if "otpauth://" in script:
            return {"success": True, "result": json.dumps(state.keys)}
        return {"success": True, "result": json.dumps(state.controls)}
    monkeypatch.setattr(vault, "_eval_js", Mock(side_effect=read))

    def write(task, script):
        events.append("write")
        written.append(script)
        if '"indices": [' in script:  # the reset writer answers one of its outcome tokens
            return {"success": True, "result": TOKENS["mask:" + "1" * script.split('"indices": [')[1].split("]")[0].count(",") + "1"]}
        return {"success": True, "result": json.dumps({"filled": 1, "reasons": ["written"]})}
    monkeypatch.setattr(vault, "_eval_js_secret", Mock(side_effect=write))
    monkeypatch.setattr(signup, "outcome_tokens", lambda: dict(TOKENS))
    redacted = []
    monkeypatch.setattr("agent.redact.register_vault_redaction_value",
                        lambda value, **kw: events.append("redact") or redacted.append(value))
    # the handler path without the browser fences (covered by their own tests)
    monkeypatch.setattr(vault, "_fenced_page_op", lambda tid, fn: vault.typed_page_op(fn))
    monkeypatch.setattr(vault, "_outside_payment_lock", lambda tid, op: op())
    return SimpleNamespace(events=events, written=written, backend=backend, meta=meta, state=state, redacted=redacted)


def call(action, **args):
    """The tool as the agent calls it: registry dispatch of browser_vault."""
    return json.loads(registry.dispatch("browser_vault", {"action": action, **args}, task_id="t1"))


def saved_password(page):
    return page.backend.resolve_password.return_value


def test_reset_generates_and_saves_the_password_before_it_is_written_and_never_returns_it(page, caplog):
    caplog.set_level(logging.DEBUG)
    out = call("reset_password", identifier="jane@x.test", label="Shop")
    assert out["success"] and out["saved"] and out["created"] and out["handle"] == "op:vault-h:new"
    assert page.events == ["save", "redact", "write"]  # op generates it: nothing to redact until it is read back
    assert GENERATED in page.redacted
    assert page.backend.generate_login.call_args.kwargs == {
        "origin": ORIGIN, "username": "jane@x.test", "title": "Shop", "length": 20, "symbols": True}
    assert json.dumps(GENERATED) in page.written[0] and out["filled_fields"] == 2  # new password + confirmation
    assert GENERATED not in json.dumps(out) and GENERATED not in caplog.text


def test_reset_of_an_existing_item_updates_that_item(page):
    out = call("reset_password", handle=HANDLE)
    assert out["success"] and out["handle"] == HANDLE and out["created"] is False
    assert page.backend.generate_login.call_args.args == (HANDLE,)


def test_the_field_length_and_the_symbols_option_reach_the_generator(page):
    page.state.controls = [new_password_field(maxLength=12), dict(CONFIRM, maxLength=12)]
    call("reset_password", handle=HANDLE, symbols=False)
    kwargs = page.backend.generate_login.call_args.kwargs
    assert (kwargs["length"], kwargs["symbols"]) == (12, False)


@pytest.mark.parametrize("failure", [BackendError("1Password", "op failed"), RuntimeError("op: boom")])
def test_a_failed_save_writes_nothing_to_the_page(page, failure, caplog):
    page.backend.generate_login.side_effect = failure
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is False and out["saved"] is False and "Nothing was filled" in out["error"]
    assert "write" not in page.events
    page.backend.resolve_password.assert_not_called()
    assert "boom" not in json.dumps(out) + caplog.text


@pytest.mark.parametrize("transport", ["error", "raise", "garbled"])
def test_a_failed_write_after_the_save_says_the_item_was_saved(page, transport):
    def write(task, script):
        if transport == "raise":
            raise RuntimeError("cdp closed " + script)
        return {"success": False, "error": script} if transport == "error" else {"success": True, "result": "x"}
    vault._eval_js_secret.side_effect = write
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is False and out["saved"] is True and out["handle"] == HANDLE
    assert out["error_type"] == "reset_fill_uncertain" and "Do not submit" in out["next"]
    assert "do not retry the fill" not in out["error"] and "once more" in out["next"]  # the failure text says retry once
    assert saved_password(page) not in json.dumps(out)


@pytest.mark.parametrize("trouble", ["uncertain_fill", "origin_moved", "page_unavailable_after_save"])
def test_a_retry_after_an_uncertain_reset_fills_the_same_saved_password_never_a_new_one(page, trouble):
    """Codex B1 / Opus B3: the first request may have reached the site, so a retry fills what was saved."""
    working = vault._eval_js_secret.side_effect
    if trouble == "uncertain_fill":
        vault._eval_js_secret.side_effect = RuntimeError("cdp closed")
    else:
        seen = iter(["https://elsewhere.example" if trouble == "origin_moved" else None])
        def origin(task):
            value = next(seen, ORIGIN)
            if value is None:
                raise vault._PageUnavailable("page_unavailable", "no page")
            return value
        vault._current_page_origin.side_effect = origin
    first = call("reset_password", handle=HANDLE)
    assert first["saved"] is True and first["handle"] == HANDLE and first["success"] is False
    assert not (first.get("next") or "").startswith("Submit")
    vault._eval_js_secret.side_effect = working
    vault._current_page_origin.side_effect = None
    vault._current_page_origin.return_value = ORIGIN
    retry = call("reset_password", handle=first["handle"])
    assert retry["success"] is True
    assert page.backend.generate_login.call_count == 1  # the retry did not generate another password
    assert json.dumps(GENERATED) in page.written[-1]


def test_rotate_generates_a_new_password_after_an_uncertain_one(page):
    working = vault._eval_js_secret.side_effect
    vault._eval_js_secret.side_effect = RuntimeError("cdp closed")
    call("reset_password", handle=HANDLE)
    vault._eval_js_secret.side_effect = working
    assert call("reset_password", handle=HANDLE, rotate=True)["success"] is True
    assert page.backend.generate_login.call_count == 2


def test_symbols_false_after_an_uncertain_reset_generates_a_new_password(page):
    """r2 Low: symbols=false means the site refused the saved password, so the pinned one is not filled again."""
    working = vault._eval_js_secret.side_effect
    vault._eval_js_secret.side_effect = RuntimeError("cdp closed")
    call("reset_password", handle=HANDLE)
    vault._eval_js_secret.side_effect = working
    assert call("reset_password", handle=HANDLE, symbols=False)["success"] is True
    assert page.backend.generate_login.call_count == 2
    assert page.backend.generate_login.call_args.kwargs["symbols"] is False


def test_symbols_false_again_fills_the_saved_password_that_already_has_none(page):
    """r3 Low 5: only a pending password WITH symbols is replaced when the call asks for none."""
    working = vault._eval_js_secret.side_effect
    vault._eval_js_secret.side_effect = RuntimeError("cdp closed")
    call("reset_password", handle=HANDLE, symbols=False)
    vault._eval_js_secret.side_effect = working
    assert call("reset_password", handle=HANDLE, symbols=False)["success"] is True
    assert page.backend.generate_login.call_count == 1


def test_a_reset_that_was_filled_does_not_pin_the_next_one_to_the_old_password(page):
    assert call("reset_password", handle=HANDLE)["success"] is True
    assert call("reset_password", handle=HANDLE)["success"] is True
    assert page.backend.generate_login.call_count == 2


def test_a_saved_password_that_cannot_be_read_back_is_reported_saved_and_not_filled(page):
    page.backend.resolve_password.side_effect = BackendNetwork("1Password", "timeout")
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is False and out["saved"] is True and out["handle"] == HANDLE
    assert "write" not in page.events


def test_reset_refuses_before_saving(page):
    assert call("reset_password")["error_type"] == "identifier_required"
    page.state.controls = [new_password_field(name="current-password", label="Current password",
                                              autocomplete="current-password")]
    assert call("reset_password", handle=HANDLE)["error_type"] == "reset_target_unsupported"
    page.meta.allowed_origins = ["https://other.example"]
    assert call("reset_password", handle=HANDLE)["error_type"] == "origin_mismatch"
    page.backend.generate_login.assert_not_called()
    vault._eval_js_secret.assert_not_called()


@pytest.mark.parametrize("controls", [
    [new_password_field(pageUrl=ORIGIN + "/register", formAction=ORIGIN + "/register", label="Password"),
     new_password_field(index=2, pageUrl=ORIGIN + "/register", formAction=ORIGIN + "/register",
                        name="confirm-password", label="Confirm password")],
    [new_password_field(name="registrationPassword", label="Password")],
    [new_password_field(pageUrl=ORIGIN + "/welcome", formAction=ORIGIN + "/welcome", formText="Create your account")],
    [new_password_field(pageUrl=ORIGIN + "/register?next=/reset", formAction=ORIGIN + "/register",
                        formText="Reset your password")],
], ids=["register-route", "registration-field", "form-text", "register-route-beats-reset-words"])
def test_a_registration_page_is_not_a_reset_and_nothing_is_saved(page, controls):
    """Opus A1: autocomplete=new-password alone must not turn a sign-up form into a no-approval reset."""
    page.state.controls = controls
    out = call("reset_password", identifier="jane@x.test")
    assert out["error_type"] == "registration_page"
    page.backend.generate_login.assert_not_called()
    vault._eval_js_secret.assert_not_called()


def _form(path, action="", label="Password", text="", form=True):
    """One password field as the inspection reports it: ``text`` is its form text (links and plain buttons already
    left out by the inspection; None = oversized or unreadable)."""
    return [new_password_field(pageUrl=ORIGIN + path, formAction=ORIGIN + action if action else "", label=label,
                               name="password", formText=text, formIndex=0 if form else None)]


# The approach review's probes and the r3 review's signup forms: each one filled a sign-up form with no approval on
# 5a12e6985c (or refused the last two real reset pages).
_SIGNUP_FORMS = {
    "modal-forgot": _form("/?modal=forgot-password", text="Create your account Email Password Sign up Already have "
                          "an account?", form=False),
    "hash-forgot": _form("/#/forgot", text="Sign up Email Password Create account", form=False),
    "return_to": _form("/users/new?return_to=/password/reset", "/users", text="Sign up Email Password"),
    "preset": _form("/welcome?plan=preset", text="Create your account Password", form=False),
    "create-a-password-unreadable": _form("/", label="Create a password", text=None, form=False),
    "get-started": _form("/get-started", label="Choose a password", text="Get started Email Choose a password "
                         "Continue"),
    "start-free-trial": _form("/start", label="Set your password", text="Start your free trial Work email Set your "
                              "password Continue"),
    "hostile-reset-path": _form("/reset", text="Create your account Name Email Password Sign up"),
    "root-posting-to-reset": _form("/", "/api/reset", text="Join us today Email Password Join"),
    "older-inspection": [{k: v for k, v in _form("/password/reset?token=abc")[0].items() if k != "formText"}],
}


@pytest.mark.parametrize("case", list(_SIGNUP_FORMS))
def test_a_signup_form_dressed_as_a_reset_is_refused_through_the_tool_and_nothing_is_saved_or_written(page, case):
    page.state.controls = _SIGNUP_FORMS[case]
    out = call("reset_password", identifier="jane@x.test")
    assert out["success"] is False and out["reason"] in ("registration_page", "form_text_unreadable")
    assert out["error_type"] == ("registration_page" if out["reason"] == "registration_page"
                                 else "reset_target_unsupported")
    page.backend.generate_login.assert_not_called()
    vault._eval_js_secret.assert_not_called()


@pytest.mark.parametrize("path", ["/login?reset=1", "/users/sign_in?reset=1", "/#/signin?reset=1", "/anmelden?reset=1"])
def test_a_sign_in_page_that_says_it_was_reset_is_refused_through_the_tool_and_nothing_is_saved(page, path):
    """r4 Low: the page path is a sign-in route; "reset" only in its query and text is not a choose-a-new-password page."""
    page.state.controls = _form(path, text="Your password has been reset. Sign in with your new password. Password",
                                form=False)
    page.state.controls[0]["autocomplete"] = ""
    out = call("reset_password", identifier="jane@x.test")
    assert out["success"] is False and out["reason"] == "login_page" and out["error_type"] == "reset_target_unsupported"
    page.backend.generate_login.assert_not_called()
    vault._eval_js_secret.assert_not_called()


@pytest.mark.parametrize("controls", [
    _form("/password/reset?token=abc", "/password/reset", label="New password",
          text="Reset your password New password Save Don't have an account?"),
    _form("/", label="New password", text="Choose a new password New password Save Don't have an account?",
          form=False),
    _form("/users/password/edit?reset_password_token=abc", "/users/password", label="New password",
          text="Change your password New password Change my password"),
], ids=["reset-with-sign-up-link", "spa-root-new-password-with-sign-up-link", "devise"])
def test_a_reset_page_whose_sign_up_offer_is_a_link_is_filled(page, controls):
    """r2 MEDIUM: "Don't have an account? Sign up" is a link, which the inspection leaves out of the form text (the
    plain-code ``context`` keeps it: the reset gate must not read that)."""
    page.state.controls = [dict(c, context=c["formText"] + " Sign up") for c in controls]
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is True and page.backend.generate_login.call_count == 1


def test_a_new_password_form_without_reset_evidence_is_not_filled(page):
    page.state.controls = [new_password_field(pageUrl=ORIGIN + "/account/security", formAction=ORIGIN + "/account/x",
                                              label="Password", name="pw", formText="Password Save")]
    out = call("reset_password", identifier="jane@x.test")
    assert (out["error_type"], out["reason"]) == ("reset_target_unsupported", "no_reset_evidence")
    page.backend.generate_login.assert_not_called()


def test_a_sign_up_label_on_a_reset_route_is_a_registration_page_and_nothing_is_saved(page):
    """Codex M1: the field's own placeholder / label names the sign-up; the route and the form text do not."""
    page.state.controls = [new_password_field(pageUrl=ORIGIN + "/reset", formAction=ORIGIN + "/reset", name="field",
                                              label="Sign up \u2014 New password", formText="Continue")]
    out = call("reset_password", identifier="jane@x.test")
    assert out["error_type"] == "registration_page" and out["reason"] == "registration_page"
    page.backend.generate_login.assert_not_called()
    vault._eval_js_secret.assert_not_called()


def test_a_sign_up_label_on_the_confirm_field_of_a_reset_route_is_a_registration_page(page):
    """r7 LOW: the registration check reads the label of every control of the write set, not only the primary."""
    page.state.controls = [new_password_field(pageUrl=ORIGIN + "/reset", formAction=ORIGIN + "/reset", name="field",
                                              label="New password", formText="Continue"),
                           dict(CONFIRM, pageUrl=ORIGIN + "/reset", formAction=ORIGIN + "/reset",
                                label="Confirm password Sign up")]
    out = call("reset_password", identifier="jane@x.test")
    assert out["error_type"] == "registration_page" and out["reason"] == "registration_page"
    page.backend.generate_login.assert_not_called()
    vault._eval_js_secret.assert_not_called()


def test_a_page_that_turns_into_a_sign_up_form_while_the_password_is_generated_is_not_filled(page):
    """Codex M2: the strict reset decision is made again on a fresh inspection right before the write."""
    signup_form = [new_password_field(name="signupPassword", label="New password", formText="Sign up"),
                   dict(CONFIRM, name="signupConfirm", formText="Sign up")]

    def save_then_swap(handle, **kw):
        page.events.append("save")
        page.state.controls = signup_form  # an SPA reuses the form while op works
        return handle

    page.backend.generate_login.side_effect = save_then_swap
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is False and out["reason"] == "registration_page" and out["error_type"] == "registration_page"
    assert out["saved"] is True and out["handle"] == HANDLE and "saved in 1Password" in out["error"]
    vault._eval_js_secret.assert_not_called()
    assert "write" not in page.events and saved_password(page) not in json.dumps(out)


def test_the_reset_write_goes_to_the_fresh_inspection_not_the_first_one(page):
    """The page re-rendered (new field indexes) while op worked: the write uses the fresh inspection's fields."""
    def save_then_move(handle, **kw):
        page.state.controls = [new_password_field(index=7), dict(CONFIRM, index=8)]
        return handle

    page.backend.generate_login.side_effect = save_then_move
    assert call("reset_password", handle=HANDLE)["success"] is True
    assert "7" in page.written[0].split('"indices": [')[1].split("]")[0] and "8" in page.written[0]


def test_a_reset_page_with_a_puzzle_captcha_says_so(page):
    """Codex Low: the fixed captcha signal reaches the reset writer's result, as it does the login fill's."""
    plain = vault._eval_js.side_effect
    vault._eval_js.side_effect = lambda task, script: (
        {"success": True, "result": "captcha_puzzle"} if "recaptcha" in script else plain(task, script))
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is True and out["challenge"] == "captcha_puzzle" and "handoff" in out["challenge_next"]


def test_an_invisible_check_on_a_non_bank_slot_points_to_shopping_and_a_forged_answer_is_ignored(page):
    plain = vault._eval_js.side_effect
    for answer, challenge in (("captcha_invisible", "captcha_invisible"), ("<b>x</b>", None)):
        vault._eval_js.side_effect = lambda task, script, a=answer: (
            {"success": True, "result": a} if "recaptcha" in script else plain(task, script))
        out = call("reset_password", handle=HANDLE)
        assert out.get("challenge") == challenge
        assert challenge is None or "shopping" in out["challenge_next"]


@pytest.mark.parametrize("slot", ["", None, "sensitive-2"])
@pytest.mark.parametrize("action", ["reset_password", "enroll_totp"])
def test_an_unknown_or_sensitive_slot_is_refused_before_anything_is_read(page, action, slot):
    """Opus B1: the bank rule fails closed: no slot id at all is refused like a sensitive one."""
    vault_route = __import__("tools.browser_use_cli", fromlist=["vault_route"]).vault_route
    vault_route.return_value = {"slot_id": slot} if slot is not None else {}
    out = call(action, handle=HANDLE)
    assert out["error_type"] == "bank_account_refused"
    vault._eval_js.assert_not_called()
    page.backend.get_meta.assert_not_called()
    assert page.events == []


@pytest.mark.parametrize("action", ["reset_password", "enroll_totp"])
def test_sensitive_slots_are_refused_before_anything_is_read(page, action):
    vault_route = __import__("tools.browser_use_cli", fromlist=["vault_route"]).vault_route
    vault_route.return_value = {"slot_id": "sensitive-2"}
    out = call(action, handle=HANDLE)
    assert out["error_type"] == "bank_account_refused"
    vault._eval_js.assert_not_called()
    page.backend.get_meta.assert_not_called()
    assert page.events == []


def test_enroll_saves_the_key_then_enters_the_code_it_mints(page, caplog):
    caplog.set_level(logging.DEBUG)
    page.state.controls = [CODE_FIELD]
    out = call("enroll_totp", handle=HANDLE)
    assert out["success"] and out["saved"] and out["key"] == "stored" and out["filled_fields"] == 1
    assert page.events[:4] == ["redact", "redact", "redact", "save"]  # every page form of the key, then the save
    assert [e for e in page.events if e != "redact"] == ["save", "mint", "write"]
    assert page.backend.add_totp.call_args.args == (HANDLE, KEY_URI)  # the URI keeps digits / period / algorithm
    assert {KEY_URI, KEY} <= set(page.redacted)
    assert '"123456"' in page.written[0]
    assert KEY not in json.dumps(out) and KEY not in caplog.text


@pytest.mark.parametrize("failure", [TotpAlreadySet("1Password"), RuntimeError(KEY_URI)])
def test_a_key_that_was_not_saved_is_never_confirmed(page, failure):
    page.state.controls = [CODE_FIELD]
    page.backend.add_totp.side_effect = failure
    out = call("enroll_totp", handle=HANDLE)
    assert out["success"] is False and out["saved"] is False and "No code was entered" in out["error"]
    page.backend.resolve_otp.assert_not_called()
    vault._eval_js_secret.assert_not_called()
    assert KEY not in json.dumps(out)


def test_a_page_that_goes_away_before_the_second_inspection_says_the_password_is_saved(page):
    reads = []
    working = vault._eval_js.side_effect

    def second_read_fails(task, script):
        if "otpauth://" not in script:
            reads.append(1)
            if len(reads) > 1:
                raise vault._PageUnavailable("page_unavailable", "no page")
        return working(task, script)
    vault._eval_js.side_effect = second_read_fails
    out = call("reset_password", handle=HANDLE)
    assert len(reads) == 2
    assert out["success"] is False and out["error_type"] == "page_unavailable"
    assert out["saved"] is True and out["handle"] == HANDLE and "reset_password once more with this handle" in out["next"]
    vault._eval_js_secret.assert_not_called()
    assert GENERATED not in json.dumps(out)


def test_enroll_without_a_readable_key_names_the_reason(page):
    page.state.keys = {"uris": [], "keys": []}
    out = call("enroll_totp", handle=HANDLE)
    assert (out["error_type"], out["reason"]) == ("totp_key_not_found", "no_key_on_page")
    page.state.keys = {"uris": [], "keys": [KEY, "KRSXG5CTMVRXEZLU"]}
    assert call("enroll_totp", handle=HANDLE)["reason"] == "several_keys_on_page"
    page.backend.add_totp.assert_not_called()


def _keys_on(page_nodes: str) -> dict:
    """What the key reader (the real script) finds on a synthetic page, run in node."""
    from tests.tools.test_browser_vault_plain_code import _NODE, _need_node
    src = _NODE.replace("@@PAGE@@", "[" + page_nodes + "]").replace("@@INSPECT@@", account.key_js("https://accounts.example.test"))
    src = src.replace("@@BETWEEN@@", "").replace("@@FILL@@", "null")
    run = subprocess.run([_need_node()], input=src, text=True, capture_output=True, timeout=30, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)["rows"]


def _text(*texts: str, tag: str = "P", **props) -> str:
    return ", ".join(f"h({json.dumps(tag)}, {json.dumps(dict(props, text=t))})" for t in texts)


@pytest.mark.parametrize("phrase", ["TWO-FACTOR AUTHENTICATION", "VERIFY AND ACTIVATE", "or enter the key manually",
                                    "UNITED STATES OF AMERICA", "SECURITYSETTINGSPAGE", "JBSWY3DPEHPK3PX-P", "JBSW Y3DP EHPK3PXP"])
def test_a_heading_or_phrase_is_not_a_key_and_a_qr_only_page_refuses_with_nothing_written(page, phrase):
    found = _keys_on(_text(phrase))
    assert found["keys"] == []
    assert account.page_key(found)[2] == "no_key_on_page"
    page.state.keys = found
    out = call("enroll_totp", handle=HANDLE)
    assert out["reason"] == "no_key_on_page"
    page.backend.add_totp.assert_not_called()
    vault._eval_js_secret.assert_not_called()


@pytest.mark.parametrize("shown", ["JBSW Y3DP EHPK 3PXP", "JBSWY3DPEHPK3PXP", "jbsw-y3dp-ehpk-3pxp", "JBSW Y3DP EHPK 3PXP ABC2"])
def test_a_key_as_the_page_shows_it_is_read_even_beside_a_phrase(shown):
    found = _keys_on(_text("TWO-FACTOR AUTHENTICATION", "or enter the key manually", shown, "UNITED STATES OF AMERICA"))
    assert found["keys"] == [shown]
    assert account.page_key(found)[2] == ""


@pytest.mark.parametrize("where", [
    'h("HEAD", {}, h("TITLE", {"text": "JBSWY3DPEHPK3PXP"}))',
    'h("SELECT", {}, h("OPTION", {"text": "JBSWY3DPEHPK3PXP"}))',
    'h("DIV", {"style": {"display": "none"}, "rect": {"width": 0, "height": 0}}, h("P", {"text": "JBSWY3DPEHPK3PXP"}))',
    'h("P", {"text": "JBSWY3DPEHPK3PXP", "style": {"visibility": "hidden"}})',
    'h("P", {"text": "JBSWY3DPEHPK3PXP", "rect": {"width": 0, "height": 0}})'])
def test_a_key_the_user_cannot_see_is_not_read(where):
    assert _keys_on(where)["keys"] == []
    assert _keys_on(where + ", " + _text("KRSXG5CTMVRXEZLU"))["keys"] == ["KRSXG5CTMVRXEZLU"]


def test_a_saved_key_whose_code_field_is_missing_says_it_was_saved(page):
    page.state.controls = []
    out = call("enroll_totp", handle=HANDLE)
    assert out["success"] is False and out["saved"] is True and out["handle"] == HANDLE
    assert "enter_code with this handle" in out["next"]


@pytest.mark.parametrize("found, stored", [
    ({"uris": [KEY_URI], "keys": ["jbsw y3dp ehpk 3pxp"]}, KEY_URI),
    ({"uris": [], "keys": ["jbsw y3dp ehpk 3pxp"]}, KEY),  # stored upper-cased, without spaces
    ({"uris": ["otpauth://hotp/x?secret=" + KEY], "keys": []}, ""),  # counter-based: not a TOTP
    ({"uris": [], "keys": ["ABCDEFGHIJKLMNOP"]}, "ABCDEFGHIJKLMNOP"),
    ({"uris": [], "keys": ["SHORT234"]}, ""),
    ("garbled", ""),
])
def test_page_key_choice(found, stored):
    assert account.page_key(found)[0] == stored


@pytest.mark.parametrize("chat", [True, False])
def test_code_messages_send_sera_to_the_mailbox_first_and_let_her_type_codes(monkeypatch, chat):
    """Owner 11-19 + D-0961: read the mailbox before asking Danil for a code; typing a code is allowed."""
    monkeypatch.setattr(vault, "_chat_session", lambda: chat)
    text = json.loads(vault._prompt_unavailable("shop.example"))["error"]
    asks = text.find("Ask the user")
    assert "mailbox" in text and (asks == -1 or text.find("mailbox") < asks)
    monkeypatch.setattr("agent.vault_login_classifier.count_codeish_controls", lambda listed: 2)
    hint = vault._no_code_field_result([])["next"]
    assert "yourself with browser tools" in hint and hint.find("mailbox") < hint.find("ask the")
    description = vault.BROWSER_VAULT_SCHEMA["description"]
    assert "CVC or code" not in description and "One-time codes are not secrets" in description


# The real extraction script on a synthetic page, in node: `els` are {tag, attrs, text}; the page's origin is set.
_PAGE_JS = r"""
const els = @@SPEC@@.map((s) => ({ tagName: s.tag.toUpperCase(), attrs: s.attrs || {}, children: [], readOnly: false,
  textContent: s.text || "", shadowRoot: null,
  getAttributeNames() { return Object.keys(this.attrs); }, getAttribute(a) { return this.attrs[a] ?? null; } }));
global.window = { location: { origin: @@ORIGIN@@ } };
global.document = { querySelectorAll: () => els };
process.stdout.write(JSON.stringify(@@SCRIPT@@));
"""


def run_key_script(script, els, origin=ORIGIN):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic page")
    src = _PAGE_JS.replace("@@SPEC@@", json.dumps(els)).replace("@@ORIGIN@@", json.dumps(origin)).replace(
        "@@SCRIPT@@", script)
    run = subprocess.run([node], input=src, text=True, capture_output=True, timeout=20, check=False)
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_the_key_script_checks_the_origin_itself_so_a_redirect_stores_nothing(page):
    """Codex A3: the page moved between the origin check and the read; its foreign key must not be saved."""
    els = [{"tag": "a", "attrs": {"href": KEY_URI}}]
    page.state.controls = [CODE_FIELD]
    vault._eval_js.side_effect = lambda task, script: {"success": True, "result": run_key_script(
        script, els, origin="https://evil.example")} if "otpauth://" in script else {
        "success": True, "result": json.dumps(page.state.controls)}
    out = call("enroll_totp", handle=HANDLE)
    assert out["error_type"] == "origin_changed"
    page.backend.add_totp.assert_not_called()
    # the same page on the expected origin is read
    vault._eval_js.side_effect = lambda task, script: {"success": True, "result": run_key_script(script, els)} \
        if "otpauth://" in script else {"success": True, "result": json.dumps(page.state.controls)}
    assert call("enroll_totp", handle=HANDLE)["success"] is True
    assert page.backend.add_totp.call_args.args == (HANDLE, KEY_URI)


def test_a_malformed_setup_link_leaks_neither_through_an_exception_nor_the_result(page, caplog):
    """Codex A2: `otpauth://[<seed>]/totp` made urlsplit raise with the seed in its message, before registration."""
    seed = "JBSWY3DPEHPK3PXP"
    els = [{"tag": "img", "attrs": {"alt": f"otpauth://[{seed}]/totp"}}]
    page.state.controls = [CODE_FIELD]
    vault._eval_js.side_effect = lambda task, script: {"success": True, "result": run_key_script(script, els)} \
        if "otpauth://" in script else {"success": True, "result": json.dumps(page.state.controls)}
    out = call("enroll_totp", handle=HANDLE)
    assert out["success"] is False and out["error_type"] == "totp_key_not_found"
    assert seed not in json.dumps(out) + caplog.text
    page.backend.add_totp.assert_not_called()


def test_enrolment_texts_keep_the_key_and_the_backup_codes_out_of_the_model_context(page):
    """Opus A2 / B1: no page text or screenshot while the key shows (call enroll_totp next), none while backup or
    recovery codes show; the schema carries it as well as the result."""
    page.state.controls = [CODE_FIELD]
    out = call("enroll_totp", handle=HANDLE)
    assert "do not read the page or take a screenshot" in out["next"] and "recovery codes" in out["next"]
    description = vault.BROWSER_VAULT_SCHEMA["description"]
    assert "call enroll_totp next" in description and "screenshot while it is visible" in description
    assert "recovery codes show" in description


# The whole enroll_totp input on a real 1Password backend over the fake op (tests/agent/fake_op.py, modelled on
# `op --help` only): judged from the fake's state, never from how add_totp builds its call.
OLD_URI = "otpauth://totp/Shop:jane?secret=KRSXG5CTMVRXEZLUKN2XAZLSKNSWC4TF"
OP_ITEM = {"id": "abc", "title": "Shop", "category": "LOGIN", "vault": {"id": "vault-h"},
           "urls": [{"href": ORIGIN + "/login", "primary": True}], "tags": ["shopping"],
           "sections": [{"id": "s1", "label": "Recovery"}],
           "fields": [{"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": "jane"},
                      {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password",
                       "value": "pw-1"},
                      {"id": "q1", "section": {"id": "s1", "label": "Recovery"}, "type": "CONCEALED", "label": "pin",
                       "value": "4711"}]}
_CASES = {  # case: (field already on the item, what the page shows)
    "new": (None, {"uris": [KEY_URI], "keys": [KEY]}),
    "spaced-replay": ({"id": "h", "label": "Hermes authenticator", "type": "OTP", "value": KEY_URI},
                      {"uris": [], "keys": ["jbsw y3dp ehpk 3pxp"]}),
    "foreign-otp": ({"id": "u", "label": "one-time password", "type": "OTP", "value": OLD_URI},
                    {"uris": [KEY_URI], "keys": []}),
    "replace": ({"id": "h", "label": "Hermes authenticator", "type": "OTP", "value": OLD_URI},
                {"uris": [KEY_URI], "keys": []}),
    "empty-otp-field": ({"id": "u", "label": "one-time password", "type": "OTP", "value": ""},
                        {"uris": [KEY_URI], "keys": []}),
}


@pytest.mark.parametrize("case", list(_CASES))
def test_enroll_totp_on_a_fake_op_keeps_the_passkey_and_every_other_field(page, tmp_path, monkeypatch, caplog, case):
    """Approach CHANGE (r2 HIGH, r1 Codex A1): the key goes in as one field assignment; a passkey `item get` never
    shows survives; the key is in one argv element and no file; the code entered is the one op minted."""
    caplog.set_level(logging.DEBUG)
    held, shown = _CASES[case]
    before = dict(OP_ITEM, fields=OP_ITEM["fields"] + ([held] if held else []))
    fake = fake_op.install(tmp_path / "op", before)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", None)
    clear_list_cache()
    backend = OnePasswordLoginBackend({"enabled": True, "binary_path": str(fake.exe)})
    backend._service_token = "service-token"
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", Mock(return_value=backend))
    monkeypatch.setattr(vault, "_meta_relisted", Mock(return_value=page.meta))
    page.state.controls, page.state.keys = [CODE_FIELD], shown
    try:
        out = call("enroll_totp", handle=HANDLE)
    finally:
        clear_list_cache()

    state, after = fake.state(), fake.item()
    assert state["passkey"] and not state["violations"] and not any(c["stdin"] for c in state["calls"])
    assert list(scratch.iterdir()) == []
    assert KEY not in json.dumps(out) + caplog.text and "KRSXG5CT" not in json.dumps(out) + caplog.text
    edits = [c["argv"] for c in state["calls"] if c["argv"][1] == "edit"]
    hermes = [(f["type"], f["value"]) for f in after["fields"] if f["label"] == "Hermes authenticator"]
    if case in ("spaced-replay", "foreign-otp"):
        assert edits == [] and after == before
    else:
        assert sum(KEY in a for c in state["calls"] for a in c["argv"]) == 1 and hermes == [("OTP", KEY_URI)]
        assert [f for f in after["fields"] if "Hermes" not in f["label"]] == before["fields"][:len(OP_ITEM["fields"])] \
            + ([held] if case == "empty-otp-field" else [])
        assert {k: v for k, v in after.items() if k != "fields"} == {k: v for k, v in before.items() if k != "fields"}
    if case == "replace":
        assert [(f["type"], f["value"]) for f in after["fields"] if f["label"].startswith("Hermes previous authenticator key ")] \
            == [("CONCEALED", OLD_URI)]
        assert sum("KRSXG5CT" in a for c in state["calls"] for a in c["argv"]) == 1
    if case == "foreign-otp":
        assert out["error_type"] == "totp_already_set" and out["saved"] is False and state["minted"] == []
        vault._eval_js_secret.assert_not_called()
    elif case == "empty-otp-field":  # which field op's --otp reads is unverified: no code, nothing confirmed
        assert out["error_type"] == "totp_code_unavailable" and out["saved"] is True
        vault._eval_js_secret.assert_not_called()
    else:
        assert out["success"] is True and out["key"] == ("already_stored" if case == "spaced-replay" else "stored")
        (code,) = state["minted"]
        assert json.dumps(code) in page.written[-1]


def _real_backend_on_fake_op(page, tmp_path, monkeypatch):
    """The page fixture's fake page with a real backend on a fake ``op``."""
    from agent.vault_backends import onepassword

    fake = fake_op.install(tmp_path / "op", OP_ITEM)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(onepassword, "_TIMEOUT", 1.0)
    clear_list_cache()
    backend = OnePasswordLoginBackend({"enabled": True, "binary_path": str(fake.exe)})
    backend._service_token = "service-token"
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", Mock(return_value=backend))
    monkeypatch.setattr(vault, "_meta_relisted", Mock(return_value=page.meta))
    return fake


@pytest.fixture
def hanging_op(page, tmp_path, monkeypatch):
    """An op whose item edits time out."""
    fake = _real_backend_on_fake_op(page, tmp_path, monkeypatch)
    fake.flag("hang")
    yield fake
    clear_list_cache()


@pytest.fixture
def network_op(page, tmp_path, monkeypatch):
    """An op that applies an item edit, then fails with a network error (not a timeout of ours)."""
    fake = _real_backend_on_fake_op(page, tmp_path, monkeypatch)
    fake.flag("late", "network")
    yield fake
    clear_list_cache()


def test_an_op_timeout_while_saving_the_password_says_it_may_have_been_saved(page, hanging_op):
    """Opus B2: after a timeout the item may be written: not "saved: false", and look in the vault before a new one."""
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is False and out["saved"] == "unknown" and out["error_type"] == "vault_network"
    assert "MAY have been saved" in out["error"] and "action=list" in out["next"] and "before saving another" in out["next"]
    assert "changed password" not in out["next"] and "without a handle" in out["next"]
    vault._eval_js_secret.assert_not_called()


def test_an_op_timeout_while_saving_the_key_says_it_may_have_been_saved(page, hanging_op):
    page.state.controls, page.state.keys = [CODE_FIELD], {"uris": [KEY_URI], "keys": [KEY]}
    out = call("enroll_totp", handle=HANDLE)
    assert out["success"] is False and out["saved"] == "unknown" and "MAY have been saved" in out["error"]
    assert KEY not in json.dumps(out) and "enroll_totp once more" in out["next"]
    vault._eval_js_secret.assert_not_called()


def test_a_network_error_after_the_password_write_was_applied_says_saved_unknown_and_list_first(page, network_op):
    """r7 LOW: any BackendNetwork on a write (not only our own timeout) may follow an applied write."""
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is False and out["saved"] == "unknown" and out["error_type"] == "vault_network"
    assert "MAY have been saved" in out["error"] and "Retry once in a minute" not in out["error"]
    assert "action=list" in out["next"] and "with that handle" in out["next"] and "without a handle" in out["next"]
    assert "changed password" not in out["next"]
    vault._eval_js_secret.assert_not_called()


def test_a_network_error_after_the_key_write_was_applied_says_saved_unknown(page, network_op):
    page.state.controls, page.state.keys = [CODE_FIELD], {"uris": [KEY_URI], "keys": [KEY]}
    out = call("enroll_totp", handle=HANDLE)
    assert out["success"] is False and out["saved"] == "unknown" and "MAY have been saved" in out["error"]
    assert "Retry once in a minute" not in out["error"] and KEY not in json.dumps(out)
    assert "enroll_totp once more" in out["next"]
    vault._eval_js_secret.assert_not_called()
