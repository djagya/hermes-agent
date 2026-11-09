"""Browser fault hunt 2026-11-07, G4 vault items: B17 (consent outcome), B19 (no_matching_field), B20 (chat remedy)."""
import json
import logging
from unittest.mock import patch

import pytest

from tests.agent.test_vault_letter import _enabled, _login, _sealed_handle  # autouse fixture + helpers
from tests.tools.test_browser_vault_signup import HANDLE, core, field, fill  # core is a fixture
from tools import browser_vault_tool as vault


def _consent_says(core, verdict, reason):
    def consent(*a, outcome=None, **kw):
        if outcome is not None:
            outcome["reason"] = reason
        return verdict
    core.consent.side_effect = consent


@pytest.mark.parametrize("verdict,reason,final", [("decline", "refused", True), ("decline", "timeout", False),
                                                  ("decline", "unavailable", False)])
def test_b17_signup_consent_outcome_only_refusal_is_final(core, caplog, verdict, reason, final):
    _consent_says(core, verdict, reason)
    with caplog.at_level(logging.INFO, logger="tools.browser_vault_signup"):
        out = fill()
    assert out["error_type"] == "signup_declined" and out["consent_outcome"] == reason
    assert ("Do not retry" in out["error"]) is final and ("approve again" in out["error"]) is not final
    core.backend.resolve_password.assert_not_called()
    assert f"vault consent surface=vault-signup outcome={reason}" in caplog.text


def test_b17_letter_origin_and_reuse_carry_the_outcome(tmp_path):
    evil = "https://beispiel-kk.de.evil.example"
    out, _, writes = _login(_sealed_handle(tmp_path), page=evil + "/login", expected_origin=evil, confirm="timeout")
    assert out["error_type"] == "origin_declined" and out["consent_outcome"] == "timeout" and not writes
    assert "Do not retry" not in out["error"]
    out, _, _ = _login(_sealed_handle(tmp_path), page=evil + "/login", expected_origin=evil, confirm=False)
    assert out["consent_outcome"] == "refused" and "Do not retry" in out["error"]


@pytest.mark.parametrize("controls,hint", [
    ([field(autocomplete="username", type="email", name="email", label="Email", index=0)], "identifier"),
    ([field(autocomplete="new-password")], "purpose=signup"),
])
def test_b19_no_matching_login_field_is_typed_with_a_hint(core, monkeypatch, controls, hint):
    monkeypatch.setattr(vault, "_eval_js", lambda *a: {"success": True, "result": json.dumps(controls)})
    out = json.loads(vault.browser_vault_fill(HANDLE))
    assert out["error_type"] == "no_matching_field" and hint in out["next"]
    core.write.assert_not_called()


@pytest.mark.parametrize("platform,chat", [("telegram", True), ("cli", False)])
def test_b20_managed_chat_save_login_names_1password_and_handoff(monkeypatch, platform, chat):
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: None)
    monkeypatch.setattr(vault, "_current_page_origin", lambda task_id: "https://acme.test")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://control.invalid")
    monkeypatch.setattr("tools.approval_context._get_session_platform", lambda: platform)
    with patch("agent.vault_backends.unlock.can_prompt_here", return_value=False):
        out = json.loads(vault.browser_vault_save_login(task_id="t1"))
    assert out["error_type"] == "prompt_unavailable"
    assert ("1Password" in out["error"] and "handoff" in out["error"]) is chat
    assert ("hermes vault add" in out["error"]) is not chat
