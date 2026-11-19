"""One helper recognises xAI inference routes: ``api.x.ai`` and the exact xai-oauth subscription proxy.

Every site that used to compare the host to ``api.x.ai`` must treat the proxy default the same way,
while lookalike hosts stay foreign (the bearer must never be taken for an xAI route).
"""
from types import SimpleNamespace

import pytest

from agent.auxiliary_client import _endpoint_default_headers, _provider_for_host, _POOL_PROVIDER_BY_HOST
from agent.chat_completion_helpers import _alias_tool_search_bridge_for_xai
from agent.codex_responses_adapter import classify_responses_route
from agent.model_metadata import _infer_provider_from_url
from agent.prompt_cache_scope import is_slot_keyed_cache_route
from hermes_cli.runtime_provider import _detect_api_mode_for_url
from tools.xai_http import XAI_GROK_PROXY_REQUIRED_HEADERS
from utils import is_xai_inference_host

API = "https://api.x.ai/v1"
PROXY = "https://cli-chat-proxy.grok.com/v1"
FOREIGN = [
    "https://cli-chat-proxy.grok.com.evil.example/v1", "https://sub.cli-chat-proxy.grok.com/v1",
    "http://cli-chat-proxy.grok.com/v1", "https://cli-chat-proxy.grok.com:444/v1",
    "https://user@cli-chat-proxy.grok.com/v1", "https://other.grok.com/v1",
    "https://evil.example/cli-chat-proxy.grok.com/v1", "https://api.x.ai.evil.example/v1", "",
]


@pytest.mark.parametrize("url", [API, PROXY, "https://cli-chat-proxy.grok.com:443/v1", "https://api.x.ai"])
def test_helper_accepts_the_two_inference_hosts(url):
    assert is_xai_inference_host(url)


@pytest.mark.parametrize("url", FOREIGN)
def test_helper_rejects_lookalikes(url):
    assert not is_xai_inference_host(url)


@pytest.mark.parametrize("url", [API, PROXY])
def test_route_flags_cache_scope_and_api_mode_treat_both_hosts_alike(url):
    agent = SimpleNamespace(provider="", base_url=url, _base_url_hostname="", _base_url_lower=url)
    assert classify_responses_route(agent).is_xai_responses
    assert is_slot_keyed_cache_route("", "m", url)
    assert _detect_api_mode_for_url(url) == "codex_responses"
    assert _infer_provider_from_url(url) == "xai"
    assert _provider_for_host(url, _POOL_PROVIDER_BY_HOST) == "xai-oauth"


@pytest.mark.parametrize("url", ["https://us-east-1.api.x.ai/v1", "https://eu.api.x.ai/v1"])
def test_aux_host_tables_keep_api_x_ai_subdomains(url):
    """The aux pool/refresh tables matched ``*.api.x.ai`` on the base release; the proxy is added, not swapped in."""
    from agent.auxiliary_client import _AUTH_REFRESH_PROVIDER_BY_HOST
    assert _provider_for_host(url, _POOL_PROVIDER_BY_HOST) == "xai-oauth"
    assert _provider_for_host(url, _AUTH_REFRESH_PROVIDER_BY_HOST) == "xai-oauth"


@pytest.mark.parametrize("url", FOREIGN)
def test_lookalikes_are_not_xai_routes_anywhere(url):
    agent = SimpleNamespace(provider="", base_url=url, _base_url_hostname="", _base_url_lower=url)
    assert not classify_responses_route(agent).is_xai_responses
    assert not is_slot_keyed_cache_route("", "m", url)
    assert _detect_api_mode_for_url(url) != "codex_responses"


@pytest.mark.parametrize("url", [API, PROXY])
def test_tool_search_alias_applies_on_both_hosts(url):
    tools = [{"type": "function", "function": {"name": "tool_search", "parameters": {}}}]
    transport = SimpleNamespace(_last_wire_aliases={})
    agent = SimpleNamespace(provider="", base_url=url, _base_url_hostname="", log_prefix="")
    out = _alias_tool_search_bridge_for_xai(agent, transport, tools)
    assert out[0]["function"]["name"] != "tool_search"
    assert transport._last_wire_aliases


def test_agent_ladder_labels_a_bare_proxy_url_as_xai_oauth():
    from agent.agent_init import _resolve_api_mode
    agent = SimpleNamespace(provider="", model="grok", _base_url_hostname="cli-chat-proxy.grok.com",
                            _base_url_lower=PROXY, api_mode=None)
    _resolve_api_mode(agent, None, None, PROXY)
    assert (agent.api_mode, agent.provider) == ("codex_responses", "xai-oauth")


def test_aux_headers_on_the_proxy_carry_the_grok_cli_headers():
    headers = _endpoint_default_headers(PROXY, "xai-oauth", xai=True)
    assert {k: headers[k] for k in XAI_GROK_PROXY_REQUIRED_HEADERS} == XAI_GROK_PROXY_REQUIRED_HEADERS
    assert not any(k in (_endpoint_default_headers(API, "xai", xai=True) or {}) for k in XAI_GROK_PROXY_REQUIRED_HEADERS)
