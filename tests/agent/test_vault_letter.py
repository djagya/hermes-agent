"""Sealed letter credentials: gateway intake + browser_vault action=letter_login.

Synthetic letter modelled on a German insurer's questionnaire letter (Versichertennummer +
Einmal-Kennwort on a type=text field). The vision model is faked; the invariants are that the secret
never reaches anything model-facing, that unclear reads and unverified sites write nothing, and that a
second fill is never automatic: a refill needs the page's failure signal, or a sign-in the backend
itself confirmed, then saw expire, plus the user's yes.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from agent import vault_letter as vl
from agent.vault_login_classifier import LoginControl

SECRET = "Kq7-xP2m"
IDENT = "A123456789"
FIRST = {
    "has_login_secret": True, "organization": "Beispiel Krankenkasse", "purpose": "Fragebogen Einkommen",
    "portal_urls": ["www.beispiel-kk.de/fragebogen"], "identifier_label": "Versichertennummer",
    "identifier": IDENT, "secret_label": "Einmal-Kennwort", "secret": SECRET, "uncertain": False,
    "summary": f"Bitte bis 30.11. ausfüllen. Ihr Kennwort {SECRET} gilt einmal.",
}
VERIFY = {"identifier": IDENT, "secret": SECRET, "uncertain": False}
PAGE = "https://login.beispiel-kk.de/fragebogen"
FORM = [
    {"index": 0, "type": "text", "name": "vsnr", "label": "Versichertennummer", "autocomplete": ""},
    {"index": 1, "type": "text", "name": "ekw", "label": "Einmal-Kennwort", "autocomplete": "off"},
    {"index": 2, "type": "text", "name": "q", "label": "Suche", "autocomplete": ""},
]


@pytest.fixture(autouse=True)
def _enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(vl, "_intake_cfg", lambda: {"enabled": True})
    vl.clear_letters()
    from agent.redact import clear_vault_redaction_values
    clear_vault_redaction_values()
    yield
    vl.clear_letters()


def _seal(tmp_path, replies):
    photo = tmp_path / "letter.jpg"
    photo.write_bytes(b"jpeg")
    calls = iter(replies)

    async def fake_ask(path, prompt):
        return next(calls)

    with patch.object(vl, "_ask_vision", side_effect=fake_ask):
        kept, notes = asyncio.run(vl.seal_letter_images([str(photo)]))
    return photo, kept, notes


def _login(handle, page=PAGE, form=FORM, **kw):
    from tools import browser_vault_tool as bvt

    writes = []

    def fake_eval(task_id, expression):
        return {"success": True, "result": page if "location.href" in expression else json.dumps(form)}

    def fake_secret(task_id, expression):
        writes.append(expression)
        return {"success": True, "result": json.dumps({"filled": expression.count('"token"')})}

    action = kw.pop("action", bvt.browser_vault_letter_login)
    with patch.object(bvt, "_focus_bound_origin", lambda *a, **k: None), \
         patch.object(bvt, "_eval_js", side_effect=fake_eval), \
         patch.object(bvt, "_eval_js_secret", side_effect=fake_secret), \
         patch.object(bvt, "_confirm_letter_origin", return_value=kw.pop("confirm", False)), \
         patch.object(bvt, "_confirm_letter_reuse", return_value=kw.pop("reuse", False)):
        raw = action(handle, task_id=kw.pop("task_id", "t"), **kw)
    return json.loads(raw), raw, writes


DRAFT = [{"index": 0, "type": "text", "name": "inc", "label": "Monatliches Einkommen", "autocomplete": ""}]


def _signed_in(handle, page=PAGE, form=DRAFT, **kw):
    from tools import browser_vault_tool as bvt
    return _login(handle, page=page, form=form, action=bvt.browser_vault_letter_signed_in, **kw)[0]


def _sealed_handle(tmp_path):
    _, _, notes = _seal(tmp_path, [FIRST, VERIFY])
    return notes[0].split("letter handle: ")[1].split()[0]


def test_letter_photo_is_sealed_and_login_fills_both_fields_without_leaking(tmp_path):
    from agent.redact import redact_sensitive_text
    from tools import browser_vault_tool as bvt

    photo, kept, notes = _seal(tmp_path, [FIRST, VERIFY])
    assert kept == [] and not photo.exists()  # pixels never routed, cached photo gone
    note = notes[0]
    assert SECRET not in note and IDENT in note and "Einmal-Kennwort" in note
    handle = note.split("letter handle: ")[1].split()[0]
    assert SECRET not in json.dumps(vl.list_letters()) and SECRET not in bvt.browser_vault_list()
    assert SECRET not in redact_sensitive_text(f"echo {SECRET}")  # egress boundary knows it

    out, raw, writes = _login(handle)
    assert out["success"] and out["filled_fields"] == 2 and out["identifier_filled"] and out["attempt"] == 1
    assert SECRET not in raw
    assert SECRET in writes[0] and '"index": 1' in writes[0] and '"index": 2' not in writes[0]

    # No blind replay: a second fill needs the page's own failure signal, and the total is capped.
    again, _, writes2 = _login(handle)
    assert again["error_type"] == "attempt_unclear" and not writes2
    assert _login(handle, after_failed_login=True)[0]["success"]
    assert _login(handle, after_failed_login=True)[0]["error_type"] == "attempts_exhausted"


@pytest.mark.parametrize("replies", [
    [FIRST, {**VERIFY, "secret": "Kq7-xP2rn"}],  # the two reads disagree (m vs rn)
    [{**FIRST, "uncertain": True}, VERIFY],       # the reader flagged a character
    [{**FIRST, "identifier": SECRET}, {**VERIFY, "identifier": SECRET}],  # code read as the number too
])
def test_unclear_read_keeps_no_secret_and_never_fills(tmp_path, replies):
    _, kept, notes = _seal(tmp_path, replies)
    assert kept == [] and "AMBIGUOUS" in notes[0] and SECRET not in notes[0] and "K q" not in notes[0]
    handle = notes[0].split("letter handle: ")[1].split()[0]
    assert vl.get_letter(handle).secret == ""
    out, _, writes = _login(handle)
    assert out["error_type"] == "letter_ambiguous" and not writes


def test_unprinted_site_needs_user_confirmation_and_plain_photos_pass_through(tmp_path):
    photo, kept, notes = _seal(tmp_path, [{"has_login_secret": False}])
    assert kept == [str(photo)] and notes == [] and photo.exists()
    # Fail closed: an answer that is not a clear boolean withholds and deletes the photo.
    photo, kept, notes = _seal(tmp_path, [{"has_login_secret": "true", "secret": SECRET}])
    assert kept == [] and not photo.exists() and "withheld" in notes[0] and SECRET not in notes[0]

    _, _, notes = _seal(tmp_path, [FIRST, VERIFY])
    handle = notes[0].split("letter handle: ")[1].split()[0]
    evil = "https://beispiel-kk.de.evil.example/login"
    out, _, writes = _login(handle, page=evil)
    assert out["error_type"] == "origin_unverified" and not writes
    out, _, writes = _login(handle, page=evil, expected_origin="https://beispiel-kk.de.evil.example")
    assert out["error_type"] == "origin_declined" and not writes
    out, _, writes = _login(handle, page=evil, expected_origin="https://beispiel-kk.de.evil.example", confirm=True)
    assert out["success"] and writes


@pytest.mark.parametrize("page,printed,allowed", [
    ("https://login.beispiel-kk.de", "www.beispiel-kk.de/x", True),
    ("http://login.beispiel-kk.de", "www.beispiel-kk.de/x", False),
    ("https://kk.github.io", "someone.github.io", False),
    ("https://someone.github.io", "someone.github.io", True),
    ("https://evil.co.uk", "co.uk", False),
])
def test_origin_policy(page, printed, allowed):
    cred = vl.LetterCredential(handle="letter-x", organization="", purpose="", portal_urls=[printed],
                               identifier_label="", identifier="", secret_label="", secret="s", status="ready",
                               expires_at=time.time() + 60)
    assert vl.origin_allowed(page, cred) is allowed


def test_field_override_and_ambiguity():
    cred = vl.LetterCredential(handle="letter-x", organization="", purpose="", portal_urls=[], identifier_label="",
                               identifier=IDENT, secret_label="", secret=SECRET, status="ready", expires_at=0)
    two = [LoginControl.from_dict(c) for c in (
        {"index": 0, "type": "text", "name": "code1", "label": "Zugangscode"},
        {"index": 1, "type": "text", "name": "code2", "label": "Zugangscode"},
    )]
    assert select_error(two, cred) == "secret_field_ambiguous"
    fills, _ = vl.select_letter_fills(two, cred, secret_field="code2")
    assert [f["index"] for f in fills if f["token"] == "letter-secret"] == [1]
    assert select_error(two, cred, secret_field="Zugangscode") == "field_ambiguous"


def select_error(controls, cred, **kw):
    return vl.select_letter_fills(controls, cred, **kw)[1].get("error_type")


@pytest.mark.parametrize("summary,kept", [
    (f"Kennwort {SECRET} gilt einmal.", "Kennwort [sealed] gilt einmal."),
    ("Kennwort K q 7 - x P 2 m gilt einmal.", ""),  # another spelling of the secret: drop the summary
])
def test_summary_never_carries_the_secret(summary, kept):
    assert vl._scrub(summary, SECRET) == kept


def test_expired_session_renews_only_a_confirmed_sign_in_with_the_users_yes(tmp_path):
    from tools import browser_vault_tool as bvt

    handle = _sealed_handle(tmp_path)
    assert _login(handle)[0]["success"]
    # An unclear submit stays blocked: no confirmation yet, and the login form alone confirms nothing.
    out, _, writes = _login(handle, session_expired=True, reuse=True)
    assert out["error_type"] == "reauth_unproven" and not writes
    assert _signed_in(handle, form=FORM)["error_type"] == "still_on_login"
    assert _signed_in(handle)["success"]

    vl.get_letter(handle).last_fill_at -= 120
    assert _login(handle, after_failed_login=True)[0]["error_type"] == "reauth_required"  # stays truthful
    out, _, writes = _login(handle, session_expired=True)
    assert out["error_type"] == "reuse_declined" and not writes
    out, raw, writes = _login(handle, session_expired=True, reuse=True)
    assert out["success"] and out["reauths_left"] == vl.MAX_REAUTHS - 1 and SECRET in writes[0] and SECRET not in raw
    # One confirmation renews one sign-in: a repeated fill cannot reset the proof.
    out, _, writes = _login(handle, session_expired=True, reuse=True)
    assert out["error_type"] == "reauth_unproven" and not writes

    assert json.loads(bvt.browser_vault_letter_done(handle))["success"]
    assert vl.get_letter(handle).secret == "" and SECRET not in bvt.browser_vault_list()
    assert _login(handle, session_expired=True, reuse=True)[0]["error_type"] == "letter_consumed"


@pytest.mark.parametrize("change,kw,error", [
    ({}, {"page": "https://www.beispiel-kk.de/login"}, "origin_mismatch"),  # printed site, other login origin
    ({}, {"task_id": "other"}, "reauth_unproven"),
    ({"signed_in_at": -vl.CHECKPOINT_MAX_AGE_S - 1}, {}, "reauth_stale"),
    ({"reauths": vl.MAX_REAUTHS}, {}, "attempts_exhausted"),
    ({"last_fill_at": 110}, {}, "rate_limited"),
    ({"expires_at": -1e9}, {}, "letter_unknown"),
])
def test_renewal_fails_closed(tmp_path, change, kw, error):
    handle = _sealed_handle(tmp_path)
    assert _login(handle)[0]["success"] and _signed_in(handle)["success"]
    cred = vl.get_letter(handle)
    cred.last_fill_at -= 120
    for name, delta in change.items():
        setattr(cred, name, getattr(cred, name) + delta)
    out, _, writes = _login(handle, session_expired=True, reuse=True, **kw)
    assert out["error_type"] == error and not writes
