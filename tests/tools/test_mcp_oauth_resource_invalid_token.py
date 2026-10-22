"""Resource rejection recovery through the native SDK and HTTP client's auth driver.

All credentials, storage and endpoints are synthetic. No browser or network is used.
"""
import asyncio
import socket
import time

import pytest
import pytest_asyncio

pytest.importorskip("mcp.client.auth.oauth2", reason="MCP OAuth SDK required")

from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

from tools import mcp_oauth as oauth
from tools import mcp_oauth_manager as manager
from tools.mcp_oauth_provider import MCPRefreshError
from tools.mcp_tool import sdk_httpx

http = sdk_httpx()
BODY = b'{"fixture":true}'
TOKENS = ("fixture-access-old", "fixture-access-new",
          "fixture-refresh-old", "fixture-refresh-new")


@pytest_asyncio.fixture
async def native_provider(tmp_path, monkeypatch):
    """Retain native storage, issuer checks, refresh fence and SDK protocol."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def no_network(*args, **kwargs):
        raise AssertionError("real network forbidden")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    class NoDiskWatcher:
        async def invalidate_if_disk_changed(self, *args, **kwargs):
            return False

    monkeypatch.setattr(manager, "get_manager", lambda: NoDiskWatcher())
    storage = oauth.HermesTokenStorage("synthetic", hermes_home=tmp_path)
    storage.bind_issuer("https://issuer.invalid")
    await storage.set_tokens(OAuthToken(
        access_token=TOKENS[0], token_type="Bearer", refresh_token=TOKENS[2],
        expires_in=3600, scope="read",
    ))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="fixture-client", issuer="https://issuer.invalid",
        redirect_uris=["http://localhost:9999/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
    ))

    async def no_callback():
        raise AssertionError("browser callback forbidden")

    provider = manager.HermesMCPOAuthProvider(
        server_url="https://resource.invalid/mcp", server_name="synthetic",
        client_metadata=OAuthClientMetadata(
            redirect_uris=["http://localhost:9999/callback"], scope="read",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], token_endpoint_auth_method="none",
        ), storage=storage, redirect_handler=oauth._make_redirect_handler(9999),
        callback_handler=no_callback,
    )
    provider.context.oauth_metadata = OAuthMetadata(
        issuer="https://issuer.invalid",
        authorization_endpoint="https://issuer.invalid/authorize",
        token_endpoint="https://issuer.invalid/token", response_types_supported=["code"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=["none"],
    )
    gate = oauth._oauth_interactive_enabled.set(False)
    try:
        yield provider, storage
    finally:
        oauth._oauth_interactive_enabled.reset(gate)
        assert provider._hermes_fence is None, "refresh fence leaked"
        assert provider.context.lock.value == 1, "SDK context lock leaked"


CASES = [
    ("positive", 200, None, ["resource", "refresh", "resource"]),
    ("no_refresh", None, "OAuthNonInteractiveError", ["resource", "prm", "asm"]),
    ("terminal_invalid_grant", None, "MCPRefreshError", ["resource", "refresh"]),
    ("terminal_invalid_client", None, "MCPRefreshError", ["resource", "refresh"]),
    ("terminal_invalid_scope", None, "MCPRefreshError", ["resource", "refresh"]),
    ("repeated_401", 401, None, ["resource", "refresh", "resource"]),
    ("issuer_mismatch", None, "OAuthNonInteractiveError", ["resource", "prm", "asm"]),
    ("scope_challenge", None, "OAuthNonInteractiveError", ["resource", "prm", "asm"]),
    ("ambiguous", None, "ReadError", ["resource", "refresh"]),
    ("cancel", None, "CancelledError", ["resource", "refresh"]),
    ("bare_401", None, "OAuthNonInteractiveError", ["resource", "prm", "asm"]),
    ("unbuffered", None, "OAuthNonInteractiveError", ["resource", "prm", "asm"]),
    ("proactive_then_401", None, "OAuthNonInteractiveError",
     ["refresh", "resource", "prm", "asm"]),
    ("peer_rotation", 200, None, ["resource", "resource"]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,status,error,expected_events", CASES,
                         ids=[case[0] for case in CASES])
async def test_resource_invalid_token(native_provider, monkeypatch, caplog, capsys,
                                      scenario, status, error, expected_events):
    provider, storage = native_provider
    events = []
    if scenario == "no_refresh":
        tokens = await storage.get_tokens()
        tokens.refresh_token = None
        await storage.set_tokens(tokens)
    elif scenario == "issuer_mismatch":
        storage.bind_issuer("https://other.invalid")
        await storage.set_tokens(await storage.get_tokens())
    elif scenario == "proactive_then_401":
        await provider._initialize()
        provider.context.token_expiry_time = time.time() - 1
    elif scenario == "peer_rotation":
        # Deterministic fence interleaving: a peer publishes a live rotated
        # pair just before this provider obtains the real file fence.
        acquire = provider._hermes_acquire_refresh_fence

        async def acquire_after_peer_rotation():
            await storage.set_tokens(OAuthToken(
                access_token=TOKENS[1], refresh_token=TOKENS[3],
                token_type="Bearer", expires_in=3600, scope="read",
            ))
            await acquire()

        monkeypatch.setattr(provider, "_hermes_acquire_refresh_fence",
                            acquire_after_peer_rotation)

    async def handler(request):
        path = request.url.path
        if path == "/token":
            events.append("refresh")
            assert provider._hermes_fence is not None
            assert request.method == "POST"
            assert b"grant_type=refresh_token" in request.content
            assert b"refresh_token=fixture-refresh-old" in request.content
            if scenario.startswith("terminal_"):
                return http.Response(400, json={"error": scenario.removeprefix("terminal_")})
            if scenario == "ambiguous":
                raise http.ReadError("synthetic delivery unknown", request=request)
            if scenario == "cancel":
                raise asyncio.CancelledError()
            return http.Response(200, json={
                "access_token": TOKENS[1], "refresh_token": TOKENS[3],
                "token_type": "Bearer", "expires_in": 3600, "scope": "read",
            })
        if path == "/mcp":
            events.append("resource")
            if scenario != "unbuffered":
                assert request.content == BODY
            assert provider._hermes_fence is None
            if (request.headers.get("authorization") == "Bearer " + TOKENS[1]
                    and scenario not in ("repeated_401", "proactive_then_401")):
                return http.Response(200, json={"ok": True})
            challenge = ('Bearer error="insufficient_scope", scope="write"'
                         if scenario == "scope_challenge" else 'Bearer error="invalid_token"')
            return http.Response(401, headers={} if scenario == "bare_401" else {
                "www-authenticate": challenge,
            })
        if "oauth-protected-resource" in path:
            events.append("prm")
            return http.Response(200, json={"resource": "https://resource.invalid/mcp",
                                          "authorization_servers": ["https://issuer.invalid"]})
        if "oauth-authorization-server" in path:
            events.append("asm")
            return http.Response(200, json=provider.context.oauth_metadata.model_dump(
                mode="json", exclude_none=True))
        raise AssertionError("unexpected synthetic endpoint")

    async def body_stream():
        yield BODY

    class StreamingMockTransport(http.MockTransport):
        async def handle_async_request(self, request):
            if scenario == "unbuffered" and request.url.path == "/mcp":
                # Model a wire transport consuming, not buffering, the body.
                body = b"".join([chunk async for chunk in request.stream])
                assert body == BODY
                with pytest.raises(http.RequestNotRead):
                    _ = request.content
                return await handler(request)
            return await super().handle_async_request(request)

    async with http.AsyncClient(transport=StreamingMockTransport(handler), auth=provider) as client:
        if error:
            # Cancellation derives from BaseException; do not turn it into a
            # success or catch unrelated failures with a broad exception type.
            exception = {"OAuthNonInteractiveError": oauth.OAuthNonInteractiveError,
                         "MCPRefreshError": MCPRefreshError,
                         "ReadError": http.ReadError,
                         "CancelledError": asyncio.CancelledError}[error]
            with pytest.raises(exception) as caught:
                await client.post("https://resource.invalid/mcp",
                                  content=body_stream() if scenario == "unbuffered" else BODY)
            if error == "MCPRefreshError":
                assert caught.value.kind == "oauth_rejected"
        else:
            response = await client.post("https://resource.invalid/mcp", content=BODY)
            assert response.status_code == status
    assert events == expected_events
    if scenario in ("positive", "repeated_401", "peer_rotation"):
        persisted = await storage.get_tokens()
        assert persisted.access_token == TOKENS[1]
        assert persisted.refresh_token == TOKENS[3]
    captured = capsys.readouterr()
    for token in TOKENS:
        assert token not in captured.out + captured.err + caplog.text
