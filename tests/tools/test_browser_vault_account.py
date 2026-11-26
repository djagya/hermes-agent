"""browser_vault reset_password / enroll_totp (J-0643, D-1085 a, D-1086 a): save first, then write the page; a
failed save writes nothing, a failed write after the save says the item was saved; the password and the key
never reach a result, a log line or an error; sensitive (bank-like) slots are refused. Fake backend and page."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.vault_backends.base import BackendNetwork
from agent.vault_backends.onepassword import TotpAlreadySet
from agent.vault_login_classifier import SIGNUP_OUTCOMES
from tools import browser_vault_account as account
from tools import browser_vault_signup as signup
from tools import browser_vault_tool as vault

ORIGIN = "https://shop.example"
HANDLE = "op:vault-h:abc"
TOKENS = {outcome: f"hv-test-{n:02d}" for n, outcome in enumerate(SIGNUP_OUTCOMES)}
KEY = "JBSWY3DPEHPK3PXP"
KEY_URI = f"otpauth://totp/Shop:jane?secret={KEY}&issuer=Shop"


def new_password_field(**changes):
    return dict({"autocomplete": "new-password", "formIndex": 0, "index": 1, "name": "password", "label": "New password",
                 "type": "password", "pageUrl": ORIGIN + "/reset-password?token=t", "rendered": True,
                 "formAction": ORIGIN + "/reset-password"}, **changes)


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
        save_login=Mock(side_effect=lambda handle, **kw: events.append("save") or (handle or "op:vault-h:new")),
        add_totp=Mock(side_effect=lambda handle, key: events.append("save") or True),
        resolve_otp=Mock(side_effect=lambda handle: events.append("mint") or "123456"))
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", Mock(return_value=backend))
    monkeypatch.setattr("agent.vault_backends.enabled_backends", Mock(return_value=[backend]))
    monkeypatch.setattr("tools.browser_use_cli.vault_route", Mock(return_value={"slot_id": "shopping"}))
    monkeypatch.setattr(vault, "_fill_page_origin", Mock(return_value=ORIGIN))
    monkeypatch.setattr(vault, "_focus_bound_origin", Mock(return_value=None))
    monkeypatch.setattr(vault, "_current_page_origin", Mock(return_value=ORIGIN))
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
    return json.loads(vault._handle_vault({"action": action, **args}, task_id="t1"))


def saved_password(page):
    return page.backend.save_login.call_args.kwargs["password"]


def test_reset_saves_the_new_password_before_it_is_written_and_never_returns_it(page, caplog):
    caplog.set_level(logging.DEBUG)
    out = call("reset_password", identifier="jane@x.test", label="Shop")
    assert out["success"] and out["saved"] and out["created"] and out["handle"] == "op:vault-h:new"
    assert page.events == ["redact", "save", "write"]
    password = saved_password(page)
    assert len(password) == 20 and password in page.redacted
    assert page.backend.save_login.call_args.kwargs | {"password": "-"} == {
        "origin": ORIGIN, "password": "-", "username": "jane@x.test", "title": "Shop"}
    assert json.dumps(password) in page.written[0] and out["filled_fields"] == 2  # new password + confirmation
    assert password not in json.dumps(out) and password not in caplog.text


def test_reset_of_an_existing_item_updates_that_item(page):
    out = call("reset_password", handle=HANDLE)
    assert out["success"] and out["handle"] == HANDLE and out["created"] is False
    assert page.backend.save_login.call_args.args == (HANDLE,)


@pytest.mark.parametrize("failure", [BackendNetwork("1Password", "dial tcp"), RuntimeError("op: {password}")])
def test_a_failed_save_writes_nothing_to_the_page(page, failure, caplog):
    def fail(handle, **kw):
        raise type(failure)(*(a.format(password=kw["password"]) for a in failure.args)) if isinstance(
            failure, RuntimeError) else failure
    page.backend.save_login.side_effect = fail
    out = call("reset_password", handle=HANDLE)
    assert out["success"] is False and out["saved"] is False and "Nothing was filled" in out["error"]
    assert "write" not in page.events
    assert saved_password(page) not in json.dumps(out) + caplog.text


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
    assert saved_password(page) not in json.dumps(out)


def test_reset_refuses_before_saving(page):
    assert call("reset_password")["error_type"] == "identifier_required"
    page.state.controls = [new_password_field(name="current-password", label="Current password",
                                              autocomplete="current-password")]
    assert call("reset_password", handle=HANDLE)["error_type"] == "reset_target_unsupported"
    page.meta.allowed_origins = ["https://other.example"]
    assert call("reset_password", handle=HANDLE)["error_type"] == "origin_mismatch"
    page.backend.save_login.assert_not_called()
    vault._eval_js_secret.assert_not_called()


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


@pytest.mark.parametrize("failure", [TotpAlreadySet("1Password"), BackendNetwork("1Password", "timeout"),
                                     RuntimeError(KEY_URI)])
def test_a_key_that_was_not_saved_is_never_confirmed(page, failure):
    page.state.controls = [CODE_FIELD]
    page.backend.add_totp.side_effect = failure
    out = call("enroll_totp", handle=HANDLE)
    assert out["success"] is False and out["saved"] is False and "No code was entered" in out["error"]
    page.backend.resolve_otp.assert_not_called()
    vault._eval_js_secret.assert_not_called()
    assert KEY not in json.dumps(out)


def test_enroll_without_a_readable_key_names_the_reason(page):
    page.state.keys = {"uris": [], "keys": []}
    out = call("enroll_totp", handle=HANDLE)
    assert (out["error_type"], out["reason"]) == ("totp_key_not_found", "no_key_on_page")
    page.state.keys = {"uris": [], "keys": [KEY, "KRSXG5CTMVRXEZLU"]}
    assert call("enroll_totp", handle=HANDLE)["reason"] == "several_keys_on_page"
    page.backend.add_totp.assert_not_called()


def test_a_saved_key_whose_code_field_is_missing_says_it_was_saved(page):
    page.state.controls = []
    out = call("enroll_totp", handle=HANDLE)
    assert out["success"] is False and out["saved"] is True and out["handle"] == HANDLE
    assert "enter_code with this handle" in out["next"]


@pytest.mark.parametrize("found, stored", [
    ({"uris": [KEY_URI], "keys": ["jbsw y3dp ehpk 3pxp"]}, KEY_URI),
    ({"uris": [], "keys": ["jbsw y3dp ehpk 3pxp"]}, "jbsw y3dp ehpk 3pxp"),
    ({"uris": ["otpauth://hotp/x?secret=" + KEY], "keys": []}, ""),  # counter-based: not a TOTP
    ({"uris": [], "keys": ["ABCDEFGHIJKLMNOP"]}, "ABCDEFGHIJKLMNOP"),
    ({"uris": [], "keys": ["SHORT234"]}, ""),
    ("garbled", ""),
])
def test_page_key_choice(found, stored):
    assert account.page_key(found)[0] == stored


def test_generated_passwords_fit_the_field():
    assert len(account.new_password(12)) == 12 and len(account.new_password(4)) == 8
    assert len(account.new_password(None)) == 20


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
