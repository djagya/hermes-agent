"""Auto-routed auxiliary calls on xAI OAuth refresh the grant after a 403 bad-credentials (#84845).

A plugin/hook ``call_llm`` with no provider override inherits the main ``xai-oauth`` route as
``resolved_provider == "auto"``; the refresh rung must still resolve the concrete backend from the
client's host so the expired bearer is refreshed and retried instead of benching the only grant.
An auto client tagged with the API-key ``xai`` backend must NOT borrow that row: an XAI_API_KEY 401
would otherwise spend a stale ``xai-oauth`` grant's refresh rotation and silently switch routes.
"""

import pytest

from agent.auxiliary_client import _auth_refresh_provider_for_route


XAI_HOSTS = ["https://api.x.ai/v1", "https://cli-chat-proxy.grok.com/v1"]


@pytest.mark.parametrize("base_url", XAI_HOSTS)
@pytest.mark.parametrize(
    ("effective_provider", "expected"),
    [("", "xai-oauth"), ("auto", "xai-oauth"), ("xai-oauth", "xai-oauth"), ("xai", "auto")],
)
def test_auto_route_on_xai_host_refreshes_xai_oauth_unless_api_key_backend(base_url, effective_provider, expected):
    assert _auth_refresh_provider_for_route("auto", base_url, effective_provider) == expected


@pytest.mark.parametrize("base_url", [
    "https://cli-chat-proxy.grok.com.evil.example/v1", "https://sub.cli-chat-proxy.grok.com/v1",
    "http://cli-chat-proxy.grok.com/v1",
])
def test_lookalike_proxy_hosts_never_refresh_the_xai_grant(base_url):
    assert _auth_refresh_provider_for_route("auto", base_url, "") == "auto"


def test_unknown_host_on_auto_route_stays_auto():
    assert _auth_refresh_provider_for_route("auto", "https://unknown.example.com/v1") == "auto"
