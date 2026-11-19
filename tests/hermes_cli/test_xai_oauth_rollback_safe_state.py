"""The xai-oauth proxy default is never stored: rolling the image back keeps a working ``api.x.ai`` route.

Stored rows (auth.json login state, credential pool, config.yaml ``model.base_url``) keep the legacy API URL;
every reader migrates it to the subscription proxy at read time (``_xai_oauth_runtime_base_url``).
A deliberate custom or env route is stored as given.
"""
import argparse
import json
from types import SimpleNamespace

import pytest

from hermes_cli.auth_constants import DEFAULT_XAI_OAUTH_BASE_URL

PROXY = "https://cli-chat-proxy.grok.com/v1"
LEGACY = "https://api.x.ai/v1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model: grok-4.6\n")
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_XAI_BASE_URL", raising=False)
    monkeypatch.delenv("XAI_BASE_URL", raising=False)
    return hermes_home


def _model_base_url(home):
    import hermes_yaml as yaml
    return (yaml.safe_load((home / "config.yaml").read_text()) or {}).get("model", {}).get("base_url")


@pytest.mark.parametrize(("value", "expected"), [
    ("", LEGACY), (PROXY, LEGACY), (PROXY + "/", LEGACY), (LEGACY, LEGACY),
    ("https://staging.x.ai/v1", "https://staging.x.ai/v1"),
])
def test_persisted_value_never_carries_the_proxy_default(value, expected):
    from hermes_cli.auth_xai import _xai_oauth_persisted_base_url
    assert _xai_oauth_persisted_base_url(value) == expected


@pytest.mark.parametrize(("env", "expected"), [
    ({}, LEGACY), ({"HERMES_XAI_BASE_URL": "https://staging.x.ai/v1"}, "https://staging.x.ai/v1"),
])
def test_device_code_login_result_stores_the_legacy_default(home, monkeypatch, env, expected):
    from hermes_cli import auth_xai
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("hermes_cli.auth._xai_oauth_discovery", lambda *_a, **_k: {"token_endpoint": "https://auth.x.ai/t"})
    monkeypatch.setattr(auth_xai, "_xai_oauth_request_device_code", lambda *_a, **_k: {
        "interval": 1, "verification_uri": "https://x.example", "user_code": "ABCD",
        "device_code": "dc", "expires_in": 60})
    monkeypatch.setattr("hermes_cli.auth._print_device_code_instructions", lambda *_a, **_k: None)
    monkeypatch.setattr("hermes_cli.auth._xai_oauth_poll_device_token", lambda *_a, **_k: {
        "access_token": "synthetic-access", "refresh_token": "synthetic-refresh"})
    creds = auth_xai._xai_oauth_device_code_login(open_browser=False)
    assert creds["base_url"] == expected
    # The same creds, read, still route to the proxy by default.
    if not env:
        assert auth_xai._xai_oauth_runtime_base_url(creds["base_url"]) == PROXY


def test_pool_seed_from_the_singleton_stores_the_legacy_url(home):
    from agent.credential_pool import load_pool
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {"xai-oauth": {
        "tokens": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}}}}))
    entry = load_pool("xai-oauth").entries()[0]
    assert entry.base_url == LEGACY
    assert entry.runtime_base_url == DEFAULT_XAI_OAUTH_BASE_URL


@pytest.mark.parametrize("login_base_url", [PROXY, "", LEGACY])
def test_auth_add_stores_the_legacy_url_whatever_the_login_returned(home, monkeypatch, login_base_url):
    from agent.credential_pool import load_pool
    from hermes_cli.auth_commands import auth_add_command
    creds = {"tokens": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                        "id_token": "", "token_type": "Bearer"},
             "discovery": {"token_endpoint": "https://auth.x.ai/token"}, "redirect_uri": "",
             "last_refresh": "2026-11-19T10:00:00Z", "source": "oauth-device-code"}
    if login_base_url:
        creds["base_url"] = login_base_url
    monkeypatch.setattr("hermes_cli.auth._xai_oauth_device_code_login", lambda **_k: creds)
    auth_add_command(SimpleNamespace(provider="xai-oauth", auth_type="oauth", api_key=None, label="a",
                                     timeout=None, no_browser=True))
    entry = load_pool("xai-oauth").entries()[0]
    assert entry.base_url == LEGACY
    assert entry.runtime_base_url == PROXY


def test_config_write_stores_the_legacy_url_but_keeps_custom_routes(home):
    from hermes_cli.auth import _update_config_for_provider
    _update_config_for_provider("xai-oauth", PROXY)
    assert _model_base_url(home) == LEGACY
    _update_config_for_provider("xai-oauth", "https://staging.x.ai/v1")
    assert _model_base_url(home) == "https://staging.x.ai/v1"
    _update_config_for_provider("xai-oauth", "")
    assert _model_base_url(home) is None


def test_other_providers_keep_their_stored_url_verbatim(home):
    from hermes_cli.auth import _update_config_for_provider
    _update_config_for_provider("openai-codex", PROXY)
    assert _model_base_url(home) == PROXY


def test_model_flow_stores_the_legacy_url_in_config(home, monkeypatch):
    from hermes_cli import main as main_mod
    monkeypatch.setattr("hermes_cli.auth.get_xai_oauth_auth_status", lambda: {"logged_in": True})
    monkeypatch.setattr("hermes_cli.setup._curses_prompt_choice", lambda *_a, **_k: 0)
    monkeypatch.setattr("hermes_cli.auth.resolve_xai_oauth_runtime_credentials", lambda *a, **k: {"base_url": PROXY})
    monkeypatch.setattr("hermes_cli.models.provider_model_ids", lambda *_a, **_k: ["grok-4.6"])
    monkeypatch.setattr("hermes_cli.auth._prompt_model_selection", lambda *_a, **_k: "grok-4.6")
    main_mod._model_flow_xai_oauth({}, current_model="grok-4.6", args=argparse.Namespace(no_browser=True, timeout=3))
    assert _model_base_url(home) == LEGACY
