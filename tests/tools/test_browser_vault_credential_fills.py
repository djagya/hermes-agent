"""Credential fills that work (fault hunt 2026-11-18, batch E): login result honesty, field choice, the lone
confirm-password step-up (D-0361), signup reach (D-0362/D-0363), purpose=reset (D-0364), stored recovery codes
(D-0365), multilingual one-time codes, vault list skips and re-list, frames, records. Synthetic pages only."""
import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.vault_login_classifier import (
    LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_login_control,
    classify_login_controls, classify_otp_controls_detailed, select_password_fill_detailed,
    select_signup_targets_detailed,
)
from tests.tools.test_browser_vault_signup import BIND, TOKENS, _run, _signup_script
from tools import browser_vault_signup as signup
from tools import browser_vault_tool as vault

ORIGIN = "https://app.dataforseo.com"
SECRET = "synthetic-cred-password"
HANDLE = "synthetic-login"


def ctl(**kw):
    base = {"autocomplete": "", "formIndex": 0, "index": 0, "label": "", "name": "", "type": "text",
            "rendered": True, "visible": True, "pageUrl": ORIGIN + "/", "formAction": ""}
    base.update(kw)
    return base


def pw(**kw):
    return ctl(**dict({"type": "password", "name": "password", "autocomplete": "current-password", "index": 1}, **kw))


def new_pw(path="/register", **kw):
    return ctl(**dict({"type": "password", "name": "password", "label": "Password", "autocomplete": "", "index": 1,
                       "pageUrl": ORIGIN + path, "formAction": ORIGIN + path}, **kw))


def L(*raw):
    return [LoginControl.from_dict(r) for r in raw]


@pytest.fixture
def core(monkeypatch):
    events = []
    meta = SimpleNamespace(kind="login", label="Synthetic", origin=ORIGIN, allowed_origins=[ORIGIN], has_otp=False,
                           identifier="", id=HANDLE)
    backend = SimpleNamespace(name="synthetic", display_name="Synthetic", needs_unlock=False,
                              get_meta=Mock(return_value=meta),
                              resolve_password=Mock(side_effect=lambda h: events.append("resolve") or SECRET))
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", Mock(return_value=backend))
    monkeypatch.setattr(vault, "_fill_page_origin", Mock(return_value=ORIGIN))
    monkeypatch.setattr(vault, "_current_page_origin", Mock(return_value=ORIGIN))
    monkeypatch.setattr(vault, "_page_backend", Mock(return_value=None))  # the CDP supervisor transport
    page = {"controls": [pw()], "frames": []}

    def _inspect(task, expr):
        if "iframe, frame" in expr and "data-hermes-vault-slot" not in expr:
            return {"success": True, "result": json.dumps(page["frames"])}
        return {"success": True, "result": json.dumps(page["controls"])}

    inspect = Mock(side_effect=_inspect)
    monkeypatch.setattr(vault, "_eval_js", inspect)
    consent = Mock(side_effect=lambda *a, **kw: events.append("consent:" + kw.get("surface", "")) or "accept")
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", consent)
    monkeypatch.setattr("agent.redact.register_vault_redaction_value", Mock())
    monkeypatch.setattr(signup, "outcome_tokens", lambda: dict(TOKENS))
    answer = {"login": {"filled": 1, "reasons": ["written"], "retained": 1}}

    def _write(task, script):
        events.append("write")
        indices = re.search(r'"indices": \[([^\]]*)\]', script)
        if indices is not None:
            return {"success": True, "result": TOKENS["mask:" + "1" * len(indices.group(1).split(","))]}
        return {"success": True, "result": json.dumps(answer["login"])}

    write = Mock(side_effect=_write)
    monkeypatch.setattr(vault, "_eval_js_secret", write)
    return SimpleNamespace(**locals())


def fill(**kw):
    return json.loads(vault.browser_vault_fill(HANDLE, **kw))


# ── 1. login fill result honesty ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", ["field_missing", "not_password", "detached", "value_rejected"])
def test_a_login_write_that_did_not_land_is_typed_with_its_reason_and_never_says_submit(core, reason):
    core.answer["login"] = {"filled": 0, "reasons": [reason]}
    out = fill()
    assert out["success"] is False and out["error_type"] == "login_fill_not_written" and out["reason"] == reason
    assert "Submit" not in out.get("next", "") and SECRET not in json.dumps(out)


def test_a_password_the_page_clears_after_the_settle_is_not_retained(core):
    core.answer["login"] = {"filled": 1, "reasons": ["written"], "retained": 0}
    out = fill()
    assert out["success"] is False and out["error_type"] == "password_not_retained"
    assert out["partially_filled"] is True and "Do not submit" in out["error"]


def test_a_page_that_navigates_during_the_settle_is_a_success_with_retention_unknown(core):
    # Review E (L5): the site auto-submitted after the write landed.
    core.answer["login"] = {"filled": 1, "reasons": ["written"], "navigated": True}
    out = fill()
    assert out["success"] is True and out["navigated_after_write"] is True and out["retention_verified"] is None
    assert "moved on" in out["next"]


_UNLOAD = ("const ls = {}; window.addEventListener = (t, f) => { (ls[t] = ls[t] || []).push(f); };"
           "later(() => (ls.pagehide || []).forEach((f) => f({ isTrusted: %s })));")


def test_the_real_login_writer_answers_when_the_page_unloads_during_the_settle():
    controls = [dict(pw(), index=1)]
    script = build_fill_js([{"index": 1, "token": "current-password", "value": SECRET}], ORIGIN, "nonce",
                           settle_ms=300)
    gone = _run(script, controls, _UNLOAD % "true", expected=SECRET)
    assert json.loads(gone["raw"]) == {"filled": 1, "reasons": ["written"], "navigated": True}
    forged = _run(script, controls, _UNLOAD % "false", expected=SECRET)  # a page-dispatched event is ignored
    assert json.loads(forged["raw"]) == {"filled": 1, "reasons": ["written"], "retained": 1}


def test_a_retained_login_fill_succeeds_and_the_writer_settles(core):
    out = fill()
    assert out["success"] is True and out["retention_verified"] is True
    script = core.write.call_args.args[1]
    assert "retained" in script and "settleMs = 1000" in script


def test_a_plugin_page_backend_login_write_stays_synchronous(core):
    # BrowserPageBackend.evaluate need not await a promise: no settle there, retention reported unverified.
    core.monkeypatch.setattr(vault, "_page_backend", Mock(return_value=object()))
    core.answer["login"] = {"filled": 1, "reasons": ["written"]}
    out = fill()
    assert out["success"] is True and out.get("retention_verified") is None
    assert "settleMs = 0" in core.write.call_args.args[1]


def test_the_real_login_writer_reads_back_after_the_settle():
    controls = [dict(pw(), index=1)]
    script = build_fill_js([{"index": 1, "token": "current-password", "value": SECRET}], ORIGIN, "nonce",
                           settle_ms=20)
    kept = _run(script, controls, expected=SECRET)
    assert json.loads(kept["raw"]) == {"filled": 1, "reasons": ["written"], "retained": 1}
    cleared = _run(script, controls, "byIdx[1].on('change', () => later(() => { byIdx[1].held = ''; }));",
                   expected=SECRET)
    assert json.loads(cleared["raw"]) == {"filled": 1, "reasons": ["written"], "retained": 0}


# ── 2. field choice ────────────────────────────────────────────────────────────────────────────────────────

PYPI_MANAGE = [
    ctl(index=3, formIndex=3, name="email email", type="email", autocomplete="email"),
    pw(index=5, formIndex=4, name="password password", label="Old password"),
    ctl(index=7, formIndex=4, name="new_password", type="password", autocomplete="new-password"),
    ctl(index=8, formIndex=4, name="password_confirm", label="Confirm password", type="password",
        autocomplete="new-password"),
    ctl(index=20, formIndex=9, name="confirm_password ", type="password", autocomplete="off", modal=True),
]


def test_an_open_dialog_holds_the_only_candidates_pypi_step_up():
    controls = L(*PYPI_MANAGE)
    fills, reason = select_password_fill_detailed(classify_login_controls(controls), SECRET, controls)
    assert [f["index"] for f in fills] == [20] and reason == ""


def test_a_dialog_whose_password_field_is_not_a_login_writes_nothing_behind_it():
    controls = L(pw(index=1), ctl(index=2, formIndex=1, name="new_password", type="password",
                                  autocomplete="new-password", modal=True))
    fills, reason = select_password_fill_detailed(classify_login_controls(controls), SECRET, controls)
    assert fills == [] and reason == "password_outside_open_dialog"


def test_a_newsletter_dialog_without_a_password_field_does_not_block_the_login_form(core):
    # Review E (L3): only a dialog that holds a password input restricts the choice.
    core.page["controls"] = [ctl(index=0, name="email", type="email"), pw(index=1),
                             ctl(index=2, formIndex=1, name="newsletter_email", type="email", modal=True)]
    out = fill()
    assert out["success"] is True and '"index": 1' in core.write.call_args.args[1]


def test_the_field_on_top_wins_over_a_covered_one():
    controls = L(pw(index=1, formIndex=0, topmost=False), pw(index=2, formIndex=1, topmost=True))
    fills, _ = select_password_fill_detailed(classify_login_controls(controls), SECRET, controls)
    assert [f["index"] for f in fills] == [2]


def test_equal_password_fields_in_two_forms_fill_the_first_like_before(core):
    # Review E (L4), owner direction: a header quick-login and the main form; the first shown one is filled.
    core.page["controls"] = [ctl(index=0, name="username"), pw(index=1, formIndex=0),
                             ctl(index=2, formIndex=1, name="username"), pw(index=3, formIndex=1)]
    out = fill()
    assert out["success"] is True and '"index": 1' in core.write.call_args.args[1]


def test_equal_password_fields_in_two_documents_are_ambiguous(core):
    core.page["controls"] = [pw(index=1, formIndex=0), pw(index=2, formIndex=1, pageUrl=ORIGIN + "/frame/login")]
    out = fill()
    assert out["error_type"] == "password_field_ambiguous"
    core.write.assert_not_called()


def test_a_readonly_until_focus_password_is_listed_by_the_real_inspection():
    controls = [dict(pw(), index=1)]
    observed = _run(build_inspection_js("nonce"), controls, "byIdx[1].readOnly = true;", register=False)
    listed = json.loads(observed["raw"])
    assert len(listed) == 1 and listed[0]["type"] == "password" and listed[0]["readonly"] is True
    assert listed[0]["modal"] is False and listed[0]["search"] is False


# ── 3. D-0361 lone confirm-password ────────────────────────────────────────────────────────────────────────

def test_a_lone_confirm_password_is_a_current_password_target():
    lone = L(ctl(index=4, formIndex=3, name="confirm_password", type="password", autocomplete="off"))
    classified = classify_login_controls(lone)
    assert [(c.token, c.score) for c in classified] == [("current-password", 80)]
    assert classify_login_control(lone[0]) is None  # the per-control classifier alone still excludes it


@pytest.mark.parametrize("partner", [
    ctl(index=3, formIndex=3, name="password", type="password", autocomplete="new-password"),
    ctl(index=3, formIndex=3, name="password", type="password"),
])
def test_a_confirm_password_with_a_partner_in_its_form_stays_excluded(partner):
    controls = L(partner, ctl(index=4, formIndex=3, name="confirm_password", type="password"))
    assert all(c.control.index != 4 for c in classify_login_controls(controls))


@pytest.mark.parametrize("name", ["confirm_new_password", "repeat_password", "password_again"])
def test_new_or_repeat_wording_is_never_a_lone_confirm(name):
    assert classify_login_controls(L(ctl(index=4, name=name, type="password"))) == []


# ── 4. signup reach (D-0362, D-0363) ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("controls", [
    [new_pw()],  # no autocomplete at all
    [new_pw(autocomplete="off")],
    [new_pw("/konto/registrieren")], [new_pw("/inscription")], [new_pw("/registreren")],
    [new_pw("/registro")], [new_pw("/registrazione")],
    [new_pw("/account", formAction=ORIGIN + "/account?action=register")],
    [new_pw(formIndex=None, formAction="")],  # SPA form without <form>
    [new_pw("/signup", name="user[password]")],  # GitHub
    [new_pw("/", name="pre_profile[password]", context="Register for free Email Password Register")],  # Trustindex
    [new_pw("/latest", name="new-account-password", formIndex=None, formAction="")],  # Discourse modal
    [new_pw(), ctl(index=5, formIndex=1, name="email", type="email", autocomplete="email")],  # newsletter
    [new_pw(), ctl(index=5, formIndex=None, name="q", type="search", search=True)],
    [new_pw(), new_pw(index=2, name="password_confirmation", label="Confirm password", autocomplete="")],
])
def test_signup_reaches_real_registration_forms(core, controls):
    core.page["controls"] = controls
    out = fill(purpose="signup")
    assert out["success"] is True and out["purpose"] == "signup", out
    assert core.events[0] == "consent:vault-signup"


@pytest.mark.parametrize("controls,reason", [
    ([new_pw("/login")], "login_route"), ([new_pw("/login?next=/signup")], "login_route"),
    ([new_pw("/reset-password")], "recovery_route"), ([new_pw("/about")], "no_registration_evidence"),
    ([new_pw(autocomplete="current-password")], "field_is_current_password"),
    ([new_pw(), new_pw(index=2), new_pw(index=3)], "too_many_password_fields"),
    ([new_pw(), new_pw(index=2, name="other")], "password_pair_unclear"),
    ([new_pw(), ctl(index=3, name="otp", label="Verification code")], "code_field_present"),
    ([new_pw(formAction="https://other.test/register")], "form_posts_elsewhere"),
])
def test_every_signup_refusal_names_a_fixed_reason(core, controls, reason):
    core.page["controls"] = controls
    out = fill(purpose="signup")
    assert out["error_type"] == "signup_target_unsupported" and out["reason"] == reason
    core.consent.assert_not_called()
    core.backend.resolve_password.assert_not_called()


def test_purpose_login_on_a_registration_page_fills_without_a_signup_approval(core):
    core.page["controls"] = [new_pw(autocomplete="")]  # D-0363 b: allowed
    out = fill()
    assert out["success"] is True and "purpose" not in out
    core.consent.assert_not_called()


@pytest.mark.parametrize("raw,token", [
    (ctl(name="mobile-search", label="Search"), None), (ctl(name="mobileMenu"), None),
    (ctl(name="mobile_number", label="Mobile number"), "tel"), (ctl(name="phone"), "tel"),
    (ctl(name="email", type="email", search=True), None),
])
def test_mobile_is_a_phone_only_next_to_number_and_search_boxes_are_never_identifiers(raw, token):
    got = classify_login_control(LoginControl.from_dict(raw))
    assert (got.token if got else None) == token


# ── 5. purpose=reset (D-0364) ──────────────────────────────────────────────────────────────────────────────

RESET = "/u/password-reset/0123abcd"


def test_reset_has_its_own_approval_then_writes_the_saved_password(core):
    core.page["controls"] = [new_pw(RESET, name="new-account-password", label="New password")]
    out = fill(purpose="reset")
    assert out["success"] is True and out["purpose"] == "reset"
    assert core.events == ["consent:vault-reset", "resolve", "write"]
    message, description = core.consent.call_args.args
    assert ORIGIN in message and "reset" in description and "updated first" in description


@pytest.mark.parametrize("controls,reason", [
    ([new_pw("/settings"), ], "no_reset_evidence"),
    ([new_pw(RESET, name="old_password", label="Current password")], "field_is_old_password"),
    ([new_pw(RESET), new_pw(RESET, index=2), new_pw(RESET, index=3)], "too_many_password_fields"),
])
def test_reset_refusals_are_typed_and_never_resolve(core, controls, reason):
    core.page["controls"] = controls
    out = fill(purpose="reset")
    assert out["error_type"] == "reset_target_unsupported" and out["reason"] == reason
    core.consent.assert_not_called()


def test_a_declined_reset_resolves_nothing(core):
    core.page["controls"] = [new_pw(RESET)]
    core.consent.side_effect = None
    core.consent.return_value = "decline"
    assert fill(purpose="reset")["error_type"] == "reset_declined"
    core.backend.resolve_password.assert_not_called()


def test_the_page_writer_in_reset_mode_accepts_reset_names_signup_refuses_them(monkeypatch):
    monkeypatch.setattr("agent.vault_login_classifier.SIGNUP_SETTLE_MS", 20)
    # The harness page is ORIGIN/register; the reset evidence is the "New password" label.
    controls = [new_pw(name="password_reset", label="New password")]
    targets, _ = select_signup_targets_detailed(L(*controls), "reset")
    from agent.vault_login_classifier import build_signup_fill_js

    fills = [{"index": 1, "token": "x", "value": SECRET}]
    reset_js = build_signup_fill_js(fills, ORIGIN, "nonce", targets, bind=BIND, tokens=TOKENS, purpose="reset")
    signup_js = build_signup_fill_js(fills, ORIGIN, "nonce", targets, bind=BIND, tokens=TOKENS)
    assert _run(reset_js, controls)["outcome"] == "mask:1"
    assert _run(signup_js, controls)["outcome"] == "none"


def test_a_form_less_spa_signup_writes_end_to_end_in_the_page(monkeypatch):
    monkeypatch.setattr("agent.vault_login_classifier.SIGNUP_SETTLE_MS", 20)
    controls = [new_pw(formIndex=None, formAction="")]
    assert _run(_signup_script(controls), controls)["outcome"] == "mask:1"


# ── 6. stored recovery codes (D-0365) ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def recovery(core, monkeypatch, tmp_path):
    monkeypatch.setattr("agent.vault_recovery_codes._ledger_path", lambda: tmp_path / "used.json")
    from agent.vault_recovery_codes import RecoveryField

    def fields(handle, reveal=False):  # one recovery-code field; its value only on the read after the approval
        core.events.append("codes" if reveal else "labels")
        return [(RecoveryField("rc", "Recovery codes", "local=r1"), "aaaa-1111 bbbb-2222" if reveal else "")]

    core.backend.recovery_fields = Mock(side_effect=fields)
    core.page["controls"] = [ctl(index=2, name="recovery_code_value recovery_code_value",
                                 label="Enter recovery code (required)")]
    monkeypatch.setattr(vault, "_saved_login", lambda handle: (core.backend, core.meta))
    monkeypatch.setattr(vault, "_bound_page_origin", lambda task, meta, kind: ORIGIN)
    core.answer["login"] = {"filled": 1, "reasons": ["written"]}
    return core


def enter(**kw):
    return json.loads(vault.browser_vault_enter_code(HANDLE, code_source="recovery", **kw))


def test_one_recovery_code_per_approval_never_echoed_never_reused(recovery):
    first = enter()
    assert first["success"] is True and first["codes_left"] == 1 and first["source"] == "recovery_codes"
    assert recovery.events[:4] == ["labels", "consent:vault-recovery-code", "codes", "write"]
    assert "aaaa-1111" in recovery.write.call_args.args[1]
    second = enter()
    assert "bbbb-2222" in recovery.write.call_args.args[1] and second["codes_left"] == 0
    third = enter()
    assert third["error_type"] == "recovery_codes_used_up"
    assert recovery.write.call_count == 2
    assert "aaaa" not in json.dumps([first, second, third]) and "bbbb" not in json.dumps([first, second, third])


def test_a_declined_recovery_approval_reads_no_code(recovery):
    recovery.consent.side_effect = None
    recovery.consent.return_value = "decline"
    assert enter()["error_type"] == "recovery_code_declined"
    assert recovery.events == ["labels"]  # the labels before the approval; never the values
    recovery.write.assert_not_called()


# ── 7. one-time codes ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", ["Bestätigungscode", "bestaetigungscode", "Sicherheitscode eingeben",
                                   "Einmalkennwort", "TAN", "Code de vérification", "Verificatiecode",
                                   "Código de verificación", "Codice di verifica"])
def test_code_words_in_five_more_languages(label):
    otp, _ = classify_otp_controls_detailed(L(ctl(label=label)))
    assert len(otp) == 1


def test_split_boxes_are_adjacent_over_listed_fields_not_raw_indices():
    boxes = L(*[ctl(index=i * 2, name=f"d{i}", label="Verification code digit", maxLength=1) for i in range(8)])
    otp, _ = classify_otp_controls_detailed(boxes)
    fills = build_otp_fills(otp, "12345678", boxes)
    assert [f["value"] for f in fills] == list("12345678")
    assert build_otp_fills(otp, "12345678") == []  # raw indices (hidden inputs between) never matched


def test_code_like_boxes_that_do_not_classify_are_reported_with_counts_only(core, monkeypatch):
    core.page["controls"] = [ctl(index=0, name="x1", maxLength=6), ctl(index=1, name="email", type="email")]
    monkeypatch.setattr(vault, "_saved_login", lambda handle: (None, None))
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: None)
    out = json.loads(vault.browser_vault_enter_code(task_id="t"))
    assert out["error_type"] == "code_field_unrecognised" and out["codeish_inputs"] == 1
    assert "passkey" not in out["error"]


# ── 8. vault list skips and re-list ────────────────────────────────────────────────────────────────────────

def test_a_website_without_scheme_is_https_and_only_unreadable_ones_are_skipped(monkeypatch):
    # Owner (build-E open question): "pypi.org" reads as https://pypi.org, as 1Password does.
    from agent.vault_backends import onepassword

    onepassword.clear_list_cache()
    backend = onepassword.OnePasswordLoginBackend({})
    monkeypatch.setattr(backend, "is_unlocked", lambda: True)
    monkeypatch.setattr(backend, "_run", lambda *a: json.dumps([
        {"id": "a", "title": "PyPI", "category": "LOGIN", "urls": [{"href": "pypi.org"}], "vault": {"id": "v"}},
        {"id": "b", "title": "Ok", "category": "LOGIN", "urls": [{"href": "https://ok.test"}], "vault": {"id": "v"}},
        {"id": "c", "title": "Bad", "category": "LOGIN", "urls": [{"href": "https://[bad"}], "vault": {"id": "v"}}]))
    monkeypatch.setattr("agent.vault_backends.enabled_backends", lambda: [backend])
    monkeypatch.setattr("agent.vault_letter.list_letters", list)
    out = json.loads(vault.browser_vault_list())
    assert [i["label"] for i in out["items"]] == ["PyPI", "Ok"]
    assert out["items"][0]["origin"] == "https://pypi.org"
    assert out["skipped"] == [{"backend": "onepassword", "title": "Bad", "reason": "website_unreadable"}]
    onepassword.clear_list_cache()


def test_a_handle_miss_lists_once_more_without_the_cache(core):
    calls = []
    core.backend.get_meta = Mock(side_effect=lambda h: None if not calls else core.meta)
    core.backend.invalidate_listing = lambda: calls.append("relist")
    assert fill()["success"] is True and calls == ["relist"]
    core.backend.get_meta = Mock(return_value=None)
    out = fill()
    assert out["error_type"] == "vault_item_not_found"


# ── 9. frames ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_form_in_a_frame_of_another_origin_is_typed(core):
    core.page["controls"] = []
    core.page["frames"] = [ORIGIN, "https://widget.example.test"]
    out = fill()
    assert out["error_type"] == "form_in_frame" and out["frames_not_inspected"] == 2
    assert out["saved_frame_origins"] == [ORIGIN]


def test_a_code_form_in_a_frame_of_another_origin_is_typed(core, monkeypatch):
    core.page["controls"] = []
    core.page["frames"] = ["https://auth.example.test"]
    monkeypatch.setattr(vault, "_saved_login", lambda handle: (None, None))
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: None)
    out = json.loads(vault.browser_vault_enter_code(task_id="t"))
    assert out["error_type"] == "form_in_frame" and "saved_frame_origins" not in out


_SAME_ORIGIN_FRAME = """
const inner = make({index: 7, formIndex: null, type: 'password', autocomplete: 'current-password', name: 'pw'});
byIdx[7] = inner;
light.push({ tagName: 'IFRAME', attrs: {}, contentDocument: { location: { origin: spec.origin },
  querySelectorAll: (s) => select([inner], s) } });
"""


def test_a_same_origin_frame_is_inspected_and_filled_like_the_page():
    listed = json.loads(_run(build_inspection_js("nonce"), [], _SAME_ORIGIN_FRAME, register=False)["raw"])
    assert [c["index"] for c in listed] == [0] and listed[0]["type"] == "password"
    script = build_fill_js([{"index": 7, "token": "current-password", "value": SECRET}], ORIGIN, "nonce")
    observed = _run(script, [], _SAME_ORIGIN_FRAME)
    assert json.loads(observed["raw"]) == {"filled": 1, "reasons": ["written"]}


# ── 10. records ────────────────────────────────────────────────────────────────────────────────────────────

def test_one_info_line_per_fill_with_codes_and_counts_only(core, caplog, monkeypatch):
    monkeypatch.setattr(vault, "_fenced_page_op", lambda tid, fn: fn())
    core.answer["login"] = {"filled": 0, "reasons": ["detached"]}
    with caplog.at_level(logging.INFO, logger="tools.browser_vault_tool"):
        vault._handle_vault({"action": "fill", "handle": HANDLE})
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("browser_vault fill")]
    assert len(lines) == 1
    assert "error_type=login_fill_not_written" in lines[0] and "reason=detached" in lines[0]
    assert SECRET not in lines[0] and ORIGIN not in lines[0] and HANDLE not in lines[0]


def test_unknown_actions_and_missing_backends_are_typed():
    assert json.loads(vault._handle_vault({"action": "nope"}))["error_type"] == "unknown_action"
    assert json.loads(vault.browser_vault_unlock("nope"))["error_type"] == "unknown_backend"


def test_a_chat_session_is_not_told_it_is_headless(core, monkeypatch):
    core.page["controls"] = [ctl(index=0, name="otp", label="Verification code", autocomplete="one-time-code")]
    monkeypatch.setattr(vault, "_saved_login", lambda handle: (None, None))
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: None)
    monkeypatch.setattr("agent.vault_backends.unlock.get_code_prompt_callback", lambda: None)
    monkeypatch.setattr("tools.approval_context._get_session_platform", lambda: "telegram")
    monkeypatch.setattr("tools.approval_context._is_cron_approval_context", lambda: False)
    out = json.loads(vault.browser_vault_enter_code(task_id="t"))
    assert out["error_type"] == "prompt_unavailable" and "headless" not in out["error"]
    assert "send the code in chat" in out["error"]
