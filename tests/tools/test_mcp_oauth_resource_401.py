"""Resource-401 recovery contracts for the native MCP OAuth provider."""

import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "mcp.client.auth.oauth2",
    reason="MCP SDK 2.0+ required for OAuth support",
)

_RESOURCE_URL = "https://mcp.example.com/mcp"
_ISSUER = "https://idp.example.com"
_TOKEN_URL = f"{_ISSUER}/token"


def _token(access: str, refresh: str | None, *, expires_in: int = 3600):
    from mcp.shared.auth import OAuthToken

    return OAuthToken(
        access_token=access,
        token_type="Bearer",
        expires_in=expires_in,
        refresh_token=refresh,
    )


async def _provider(tmp_path, monkeypatch, *, ttl: int, refresh: str | None = "R1"):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata
    from tools.mcp_oauth_manager import get_manager, reset_manager_for_tests

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    stdin = MagicMock()
    stdin.isatty.return_value = True
    monkeypatch.setattr("tools.mcp_oauth.sys.stdin", stdin)
    reset_manager_for_tests()
    provider = get_manager().get_or_build_provider("srv", _RESOURCE_URL, {})
    provider.context.oauth_metadata = OAuthMetadata(
        issuer=_ISSUER,
        authorization_endpoint=f"{_ISSUER}/authorize",
        token_endpoint=_TOKEN_URL,
    )
    provider.context.client_info = OAuthClientInformationFull.model_validate(
        {"client_id": "client-id", "redirect_uris": ["http://localhost/cb"]}
    )
    provider.context.current_tokens = _token("A1", refresh, expires_in=max(ttl, 0))
    provider.context.token_expiry_time = time.time() + ttl
    await provider.context.storage.set_tokens(provider.context.current_tokens)
    provider._initialized = True
    return provider


def _response(httpx2, status: int, request, *, body: bytes = b"", headers=None):
    return httpx2.Response(status, content=body, headers=headers, request=request)


def _refresh_success(httpx2, request):
    return _response(
        httpx2,
        200,
        request,
        body=(
            b'{"access_token":"A2","token_type":"Bearer","expires_in":3600,'
            b'"refresh_token":"R2"}'
        ),
    )


async def _assert_complete(flow, response) -> None:
    with pytest.raises(StopAsyncIteration):
        await flow.asend(response)


@pytest.mark.asyncio
async def test_live_resource_success_does_not_refresh(tmp_path, monkeypatch):
    import httpx2

    provider = await _provider(tmp_path, monkeypatch, ttl=30)
    flow = provider.async_auth_flow(httpx2.Request("POST", _RESOURCE_URL))

    resource = await flow.asend(None)
    assert str(resource.url) == _RESOURCE_URL
    await _assert_complete(flow, _response(httpx2, 200, resource))
    assert provider.context.current_tokens.access_token == "A1"


@pytest.mark.asyncio
async def test_expired_before_dispatch_uses_native_refresh(tmp_path, monkeypatch):
    import httpx2

    provider = await _provider(tmp_path, monkeypatch, ttl=-1)
    flow = provider.async_auth_flow(httpx2.Request("POST", _RESOURCE_URL))

    refresh = await flow.asend(None)
    assert str(refresh.url) == _TOKEN_URL
    resource = await flow.asend(_refresh_success(httpx2, refresh))
    assert str(resource.url) == _RESOURCE_URL
    assert resource.headers["Authorization"] == "Bearer A2"
    await _assert_complete(flow, _response(httpx2, 200, resource))


@pytest.mark.parametrize("ttl", [1, 3600], ids=["near-expiry", "long-lived-rejected"])
@pytest.mark.asyncio
async def test_valid_at_dispatch_401_refreshes_once_before_authorization(
    tmp_path, monkeypatch, ttl
):
    import httpx2

    provider = await _provider(tmp_path, monkeypatch, ttl=ttl)
    flow = provider.async_auth_flow(httpx2.Request("POST", _RESOURCE_URL))

    first_resource = await flow.asend(None)
    refresh = await flow.asend(_response(httpx2, 401, first_resource))
    assert str(refresh.url) == _TOKEN_URL
    second_resource = await flow.asend(_refresh_success(httpx2, refresh))
    assert second_resource is first_resource
    assert second_resource.headers["Authorization"] == "Bearer A2"
    await _assert_complete(flow, _response(httpx2, 200, second_resource))
    assert provider.context.current_tokens.refresh_token == "R2"


@pytest.mark.asyncio
async def test_resource_401_without_refresh_grant_keeps_sdk_authorization_path(
    tmp_path, monkeypatch
):
    import httpx2

    provider = await _provider(tmp_path, monkeypatch, ttl=3600, refresh=None)
    flow = provider.async_auth_flow(httpx2.Request("POST", _RESOURCE_URL))

    resource = await flow.asend(None)
    discovery = await flow.asend(_response(httpx2, 401, resource))
    assert str(discovery.url) != _TOKEN_URL
    assert "/.well-known/" in str(discovery.url)
    await flow.aclose()


@pytest.mark.asyncio
async def test_resource_401_terminal_refresh_failure_does_not_start_authorization(
    tmp_path, monkeypatch
):
    import httpx2

    from tools.mcp_oauth_provider import MCPRefreshError

    provider = await _provider(tmp_path, monkeypatch, ttl=3600)
    flow = provider.async_auth_flow(httpx2.Request("POST", _RESOURCE_URL))
    resource = await flow.asend(None)
    refresh = await flow.asend(_response(httpx2, 401, resource))

    with pytest.raises(MCPRefreshError, match="oauth_rejected") as exc_info:
        await flow.asend(
            _response(
                httpx2,
                400,
                refresh,
                body=b'{"error":"invalid_grant"}',
            )
        )
    assert exc_info.value.oauth_error == "invalid_grant"
    assert provider.context.current_tokens is None


@pytest.mark.asyncio
async def test_resource_401_ambiguous_refresh_transport_is_not_replayed(
    tmp_path, monkeypatch
):
    import httpx2

    from tools.mcp_oauth_provider import MCPRefreshError

    provider = await _provider(tmp_path, monkeypatch, ttl=3600)
    flow = provider.async_auth_flow(httpx2.Request("POST", _RESOURCE_URL))
    resource = await flow.asend(None)
    refresh = await flow.asend(_response(httpx2, 401, resource))

    with pytest.raises(MCPRefreshError, match="transport_ambiguous") as exc_info:
        await flow.athrow(httpx2.ReadError("fixture-only", request=refresh))
    assert exc_info.value.ambiguous_delivery is True
    assert provider.context.current_tokens.refresh_token == "R1"


@pytest.mark.asyncio
async def test_repeated_resource_401_refreshes_only_once_then_authorizes(
    tmp_path, monkeypatch
):
    import httpx2

    provider = await _provider(tmp_path, monkeypatch, ttl=3600)
    flow = provider.async_auth_flow(httpx2.Request("POST", _RESOURCE_URL))

    first_resource = await flow.asend(None)
    refresh = await flow.asend(_response(httpx2, 401, first_resource))
    second_resource = await flow.asend(_refresh_success(httpx2, refresh))
    discovery = await flow.asend(_response(httpx2, 401, second_resource))

    assert str(refresh.url) == _TOKEN_URL
    assert str(discovery.url) != _TOKEN_URL
    assert "/.well-known/" in str(discovery.url)
    await flow.aclose()


@pytest.mark.asyncio
async def test_resource_transport_failure_propagates_without_refresh(
    tmp_path, monkeypatch
):
    import httpx2

    provider = await _provider(tmp_path, monkeypatch, ttl=3600)
    flow = provider.async_auth_flow(httpx2.Request("POST", _RESOURCE_URL))
    resource = await flow.asend(None)

    with pytest.raises(httpx2.ReadError, match="fixture-only"):
        await flow.athrow(httpx2.ReadError("fixture-only", request=resource))
    assert provider.context.current_tokens.access_token == "A1"
    assert provider._hermes_fence is None
