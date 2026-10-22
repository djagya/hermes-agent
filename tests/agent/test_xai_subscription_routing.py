"""OAuth subscription route, origin pinning, SDK and rotation seam regressions."""
import logging
import sys
from types import SimpleNamespace

import pytest

from hermes_cli.auth import DEFAULT_XAI_OAUTH_BASE_URL, _xai_validate_inference_base_url
from tools.xai_http import (
    DEFAULT_XAI_BASE_URL, XAI_GROK_PROXY_REQUIRED_HEADERS,
    apply_xai_route_headers, xai_grok_proxy_headers_for,
)

PROXY = "https://cli-chat-proxy.grok.com/v1"


@pytest.mark.parametrize("url", [PROXY, "https://cli-chat-proxy.grok.com:443/v1"])
def test_proxy_route_accepts_only_compatible_https_origin(url):
    assert _xai_validate_inference_base_url(url, fallback=DEFAULT_XAI_BASE_URL) == url
    assert xai_grok_proxy_headers_for(url) == XAI_GROK_PROXY_REQUIRED_HEADERS
    assert DEFAULT_XAI_OAUTH_BASE_URL == PROXY
    assert DEFAULT_XAI_BASE_URL == "https://api.x.ai/v1"


@pytest.mark.parametrize("url", [
    "http://cli-chat-proxy.grok.com/v1", "https://cli-chat-proxy.grok.com:444/v1",
    "https://cli-chat-proxy.grok.com:bad/v1", "https://user@cli-chat-proxy.grok.com/v1",
    "https://user:pass@cli-chat-proxy.grok.com/v1", "https://other.grok.com/v1",
    "https://cli-chat-proxy.grok.com.evil.example/v1", "https://sub.cli-chat-proxy.grok.com/v1",
    "https://api.x.ai@evil.example/v1", "https://api.x.ai:444/v1",
    "https://user@api.x.ai/v1", "https://[broken/v1",
])
def test_reject_unsafe_origin_and_never_attach_proxy_headers(url):
    assert _xai_validate_inference_base_url(url, fallback=PROXY) == PROXY
    assert xai_grok_proxy_headers_for(url) == {}


def test_headers_merge_case_insensitively_and_clear_on_route_change():
    kwargs = {"default_headers": {"X-Grok-Client-Version": "explicit", "Custom": "keep"}}
    apply_xai_route_headers(kwargs, PROXY)
    assert kwargs["default_headers"]["X-Grok-Client-Version"] == "explicit"
    assert "x-grok-client-version" not in kwargs["default_headers"]
    apply_xai_route_headers(kwargs, DEFAULT_XAI_BASE_URL)
    assert kwargs["default_headers"] == {"Custom": "keep"}


@pytest.mark.parametrize("base", [None, "https://api.x.ai/v1", "https://staging.x.ai/v1", "https://evil.example/v1"])
def test_pool_routes_and_aux_http_consumers_agree(monkeypatch, base):
    from agent.credential_pool import PooledCredential
    from agent import auxiliary_client as aux
    from tools.xai_http import resolve_xai_http_credentials
    entry = PooledCredential(id="row", provider="xai-oauth", source="manual", label="test", priority=0,
                             auth_type="oauth", access_token="synthetic-access", base_url=base)
    pool = SimpleNamespace(select=lambda: entry, has_credentials=lambda: True)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: pool)
    monkeypatch.setattr(aux, "load_pool", lambda _provider: pool)
    monkeypatch.delenv("HERMES_XAI_BASE_URL", raising=False)
    monkeypatch.delenv("XAI_BASE_URL", raising=False)
    expected = base if base and base.endswith(".x.ai/v1") else PROXY
    assert entry.runtime_base_url == expected
    assert aux._resolve_xai_oauth_for_aux() == ("synthetic-access", expected)
    assert resolve_xai_http_credentials()["base_url"] == expected
    monkeypatch.setenv("HERMES_XAI_BASE_URL", PROXY)
    assert entry.runtime_base_url == PROXY
    assert aux._resolve_xai_oauth_for_aux()[1] == PROXY
    assert resolve_xai_http_credentials()["base_url"] == PROXY


def test_primary_and_aux_sdk_construction_and_credential_rotation(monkeypatch):
    from agent import agent_runtime_helpers as runtime, auxiliary_client as aux
    from agent.client_lifecycle import ClientLifecycleMixin
    from agent.credential_pool import PooledCredential
    import agent

    # Only replace transport/SDK boundaries; exercise real route/header and lifecycle code.
    captured = []
    sdk = SimpleNamespace(OpenAI=lambda **kwargs: captured.append(kwargs) or SimpleNamespace())
    monkeypatch.setitem(sys.modules, "agent.process_bootstrap", sdk)
    monkeypatch.setattr(agent, "process_bootstrap", sdk, raising=False)
    monkeypatch.setattr(runtime, "_ra", lambda: SimpleNamespace(logger=logging.getLogger(__name__)))
    monkeypatch.setattr("agent.served_model.install_served_model_capture", lambda *_args: None)
    monkeypatch.setattr(aux, "OpenAI", sdk.OpenAI)
    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda _base: {})

    class Harness(ClientLifecycleMixin):
        provider = "xai-oauth"
        model = "grok-4.7"
        api_mode = "codex_responses"
        api_key = "synthetic-old"
        base_url = DEFAULT_XAI_BASE_URL

        def __init__(self):
            self._client_kwargs = {"api_key": self.api_key, "base_url": self.base_url}

        def _build_keepalive_http_client(self, *args, **kwargs):
            return None

        def _client_log_context(self):
            return "synthetic"

        def _replace_primary_openai_client(self, *, reason):
            self.client = runtime.create_openai_client(self, self._client_kwargs, reason=reason, shared=True)
            return True

    harness = Harness()
    row = PooledCredential(id="next", provider="xai-oauth", source="manual", label="test", priority=0, auth_type="oauth",
                           access_token="synthetic-new", base_url=PROXY)
    assert harness._swap_credential(row)
    assert harness.api_key == "synthetic-new"
    assert captured[-1]["base_url"].rstrip("/") == PROXY
    assert captured[-1]["default_headers"]["x-grok-client-version"] == "1.0.67"
    aux._create_openai_client(api_key=row.access_token, base_url=row.runtime_base_url)
    assert captured[-1]["default_headers"] == XAI_GROK_PROXY_REQUIRED_HEADERS
    row.base_url = DEFAULT_XAI_BASE_URL
    assert harness._swap_credential(row)
    assert "x-grok-client-version" not in captured[-1].get("default_headers", {})
    # A singleton token refresh must retain the credential's explicitly compatible route.
    old = harness.api_key
    monkeypatch.setattr("hermes_cli.auth.resolve_xai_oauth_runtime_credentials", lambda **kw: {
        "api_key": "synthetic-refreshed" if kw.get("force_refresh") else old,
        "base_url": PROXY,
    })
    assert harness._try_refresh_codex_client_credentials()
    assert harness.api_key == "synthetic-refreshed"
    assert harness.base_url == DEFAULT_XAI_BASE_URL
    assert "x-grok-client-version" not in captured[-1].get("default_headers", {})
