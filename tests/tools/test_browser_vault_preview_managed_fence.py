"""dlz: the Desktop in-app browser (``target='preview'``) stays outside the managed browser's fences.

Managed mode (monolith browser control) refuses every preview vault operation before any page or secret
access, on the agent path and on the key-button path (``run_preview_vault`` + ``browser_vault_fill``),
instead of silently filling the leased slot. Unmanaged installs keep upstream's preview fill/save/code,
but card, letter and protected-frame actions never run there.
"""

import json

import pytest

from agent.redact import clear_vault_redaction_values
from agent.vault_store import get_vault_store
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import browser_vault_tool
from tools.browser_vault_preview import run_preview_vault
from tools.registry import registry

ORIGIN = "https://login.example.test"


def _never(*_args, **_kwargs):
    pytest.fail("a refused preview vault operation reached a page, a supervisor or a secret")


class Window:
    def __init__(self):
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload["vault"]["operation"])
        if payload["vault"]["operation"] == "open":
            return json.dumps({"success": True, "target": "pinned-tab"})
        if payload["vault"]["operation"] == "close":
            return json.dumps({"success": True})
        _never()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from agent.vault_backends.local import LocalLoginBackend

    for name in ("_fenced_page_op", "_ensure_supervisor", "_eval_js", "_eval_js_secret"):
        monkeypatch.setattr(browser_vault_tool, name, _never)
    monkeypatch.setattr(LocalLoginBackend, "resolve_password", _never)
    monkeypatch.setattr(LocalLoginBackend, "resolve_secret", _never)
    token = set_hermes_home_override(tmp_path)
    (tmp_path / "config.yaml").write_text(json.dumps({"vault": {"onepassword": {"enabled": False},
                                                                  "bitwarden": {"enabled": False}}}))
    try:
        yield tmp_path
    finally:
        clear_vault_redaction_values()
        reset_hermes_home_override(token)


def _login():
    return get_vault_store().add_item("login", "Synthetic", {"identifier": "test-user", "identifier_type": "username",
                                                             "password": "synthetic-fence-secret"}, origin=ORIGIN)


@pytest.mark.parametrize("path", ["agent", "key_button"])
def test_managed_mode_refuses_every_preview_operation(home, monkeypatch, path):
    monkeypatch.setattr(browser_vault_tool, "_is_managed", lambda: True)
    meta, window = _login(), Window()
    if path == "agent":
        raw = registry.dispatch("browser_vault", {"action": "fill", "handle": meta.id, "target": "preview"},
                                preview_callback=window)
    else:
        raw = run_preview_vault(window, lambda: browser_vault_tool.browser_vault_fill(
            meta.id, task_id="t", with_identifier=True), target="pinned-tab")
    result = json.loads(raw)
    assert result["success"] is False and result["error_type"] == "preview_unavailable", result
    assert "open" not in window.calls


@pytest.mark.parametrize("action", ["payment_controls", "payment_click", "letter_login", "letter_signed_in"])
def test_preview_runs_only_the_upstream_operations(home, monkeypatch, action):
    monkeypatch.setattr(browser_vault_tool, "_is_managed", lambda: False)
    window = Window()
    result = json.loads(registry.dispatch("browser_vault", {"action": action, "handle": "h", "ref": "r",
                                                             "target": "preview"}, preview_callback=window))
    assert result["error_type"] == "invalid_target" and window.calls == []


def test_card_is_never_filled_into_the_preview(home, monkeypatch):
    monkeypatch.setattr(browser_vault_tool, "_is_managed", lambda: False)
    import tools.browser_vault_payment as payment
    monkeypatch.setattr(payment, "fill_payment", _never)
    card = get_vault_store().add_item("payment", "Synthetic card", {"card_number": "4242424242424242",
                                                                    "exp_month": "07", "exp_year": "2035", "cvc": "123"}, origin=ORIGIN)
    result = json.loads(registry.dispatch("browser_vault", {"action": "fill", "handle": card.id, "target": "preview"},
                                          preview_callback=Window()))
    assert result["error_type"] == "payment_destination_requires_managed_browser", result
