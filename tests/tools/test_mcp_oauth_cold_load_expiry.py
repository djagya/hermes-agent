"""Tests for cold-load token expiry tracking in MCP OAuth.

PR #11383's consolidation fixed external-refresh reloading (mtime disk-watch)
and 401 dedup, but left two underlying latent bugs in place:

1. ``HermesTokenStorage.set_tokens`` persisted only relative ``expires_in``,
   which is meaningless after a process restart.
2. The MCP SDK's ``OAuthContext._initialize`` loads ``current_tokens`` from
   storage but does NOT call ``update_token_expiry``, so
   ``token_expiry_time`` stays None. ``is_token_valid()`` then returns True
   for any loaded token regardless of actual age, and the SDK's preemptive
   refresh branch at ``oauth2.py:491`` is never taken.

Consequence: a token that expired while the process was down ships to the
server with a stale Bearer header. The server's response is provider-specific
— some return HTTP 401 (caught by the consolidation's 401 handler, which
surfaces a ``needs_reauth`` error), others return HTTP 200 with an
application-level auth failure in the body (e.g. BetterStack's "No teams
found. Please check your authentication."), which the consolidation cannot
detect.

These tests pin the contract for Fix A:
- ``set_tokens`` persists an absolute ``expires_at`` wall-clock timestamp.
- ``get_tokens`` reconstructs ``expires_in`` from ``expires_at - now`` so
  the SDK's ``update_token_expiry`` computes the correct absolute expiry.
- ``HermesMCPOAuthProvider._initialize`` seeds ``context.token_expiry_time``
  after loading, so ``is_token_valid()`` reports True only for tokens that
  are actually still valid, and the SDK's preemptive refresh fires for
  expired tokens with a live refresh_token.

Reference: Claude Code solves this via an ``OAuthTokens.expiresAt`` absolute
timestamp persisted alongside the access_token (``auth.ts:~180``).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

pytest.importorskip("mcp.client.auth.oauth2", reason="MCP SDK 1.26.0+ required")

# ---------------------------------------------------------------------------
# HermesTokenStorage — absolute expiry persistence
# ---------------------------------------------------------------------------

class TestSetTokensAbsoluteExpiry:
    def test_set_tokens_persists_absolute_expires_at(self, tmp_path, monkeypatch):
        """Tokens round-tripped through disk must encode absolute expiry."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from mcp.shared.auth import OAuthToken

        from tools.mcp_oauth import HermesTokenStorage

        storage = HermesTokenStorage("srv")
        before = time.time()
        asyncio.run(
            storage.set_tokens(
                OAuthToken(
                    access_token="a",
                    token_type="Bearer",
                    expires_in=3600,
                    refresh_token="r",
                )
            )
        )
        after = time.time()

        on_disk = json.loads(
            (tmp_path / "mcp-tokens" / "srv.json").read_text()
        )
        assert "expires_at" in on_disk, (
            "Fix A: set_tokens must record an absolute expires_at wall-clock "
            "timestamp alongside the SDK's serialized token so cold-loads "
            "can compute correct remaining TTL."
        )
        assert before + 3600 <= on_disk["expires_at"] <= after + 3600

    def test_set_tokens_without_expires_in_omits_expires_at(
        self, tmp_path, monkeypatch
    ):
        """Tokens without a TTL must not gain a fabricated expires_at."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from mcp.shared.auth import OAuthToken

        from tools.mcp_oauth import HermesTokenStorage

        storage = HermesTokenStorage("srv")
        asyncio.run(
            storage.set_tokens(
                OAuthToken(
                    access_token="a",
                    token_type="Bearer",
                    refresh_token="r",
                )
            )
        )

        on_disk = json.loads(
            (tmp_path / "mcp-tokens" / "srv.json").read_text()
        )
        assert "expires_at" not in on_disk

class TestGetTokensReconstructsExpiresIn:
    def test_get_tokens_uses_expires_at_for_remaining_ttl(
        self, tmp_path, monkeypatch
    ):
        """Round-trip: expires_in on read must reflect time remaining."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from mcp.shared.auth import OAuthToken

        from tools.mcp_oauth import HermesTokenStorage

        storage = HermesTokenStorage("srv")
        asyncio.run(
            storage.set_tokens(
                OAuthToken(
                    access_token="a",
                    token_type="Bearer",
                    expires_in=3600,
                    refresh_token="r",
                )
            )
        )

        # Wait briefly so the remaining TTL is measurably less than 3600.
        time.sleep(0.05)

        reloaded = asyncio.run(storage.get_tokens())
        assert reloaded is not None
        assert reloaded.expires_in is not None
        # Should be slightly less than 3600 after the 50ms sleep.
        assert 3500 < reloaded.expires_in <= 3600

    def test_get_tokens_returns_zero_ttl_for_expired_token(
        self, tmp_path, monkeypatch
    ):
        """An already-expired token reloaded from disk must report expires_in=0."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.mcp_oauth import HermesTokenStorage, _get_token_dir

        token_dir = _get_token_dir()
        token_dir.mkdir(parents=True, exist_ok=True)
        # Write an already-expired token file directly.
        (token_dir / "srv.json").write_text(
            json.dumps(
                {
                    "access_token": "a",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "expires_at": time.time() - 60,  # expired 1 min ago
                    "refresh_token": "r",
                }
            )
        )

        storage = HermesTokenStorage("srv")
        reloaded = asyncio.run(storage.get_tokens())
        assert reloaded is not None
        assert reloaded.expires_in == 0, (
            "Expired token must reload with expires_in=0 so the SDK's "
            "is_token_valid() returns False and preemptive refresh fires."
        )

    def test_get_tokens_legacy_file_without_expires_at_is_loadable(
        self, tmp_path, monkeypatch
    ):
        """Existing on-disk files (pre-Fix-A) must still load without crashing.

        Pre-existing token files have ``expires_in`` but no ``expires_at``.
        Fix A falls back to the file's mtime as a best-effort wall-clock
        proxy: a file whose (mtime + expires_in) is in the past clamps
        expires_in to zero so the SDK refreshes on next request. A fresh
        legacy-format file (mtime = now) keeps most of its TTL.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.mcp_oauth import HermesTokenStorage, _get_token_dir

        token_dir = _get_token_dir()
        token_dir.mkdir(parents=True, exist_ok=True)
        # Legacy-shape file (no expires_at). Make it stale by backdating mtime
        # well past its nominal expires_in.
        legacy_path = token_dir / "srv.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "access_token": "a",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "r",
                }
            )
        )
        stale_time = time.time() - 7200  # 2hr ago, exceeds 3600s TTL
        import os

        os.utime(legacy_path, (stale_time, stale_time))

        storage = HermesTokenStorage("srv")
        reloaded = asyncio.run(storage.get_tokens())
        assert reloaded is not None
        assert reloaded.expires_in == 0, (
            "Legacy file whose mtime + expires_in is in the past must report "
            "expires_in=0 so the SDK refreshes on next request."
        )

# ---------------------------------------------------------------------------
# HermesMCPOAuthProvider._initialize — seed token_expiry_time
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_seeds_token_expiry_time_from_stored_tokens(
    tmp_path, monkeypatch
):
    """Cold-load must populate context.token_expiry_time.

    The SDK's base ``_initialize`` loads current_tokens but doesn't seed
    token_expiry_time. Our subclass must do it so ``is_token_valid()``
    reports correctly and the preemptive-refresh path fires when needed.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests

    assert _HERMES_PROVIDER_CLS is not None
    reset_manager_for_tests()

    storage = HermesTokenStorage("srv")
    await storage.set_tokens(
        OAuthToken(
            access_token="a",
            token_type="Bearer",
            expires_in=7200,
            refresh_token="r",
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
    )

    from mcp.shared.auth import OAuthClientMetadata

    metadata = OAuthClientMetadata(
        redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
        client_name="Hermes Agent",
    )
    provider = _HERMES_PROVIDER_CLS(
        server_name="srv",
        server_url="https://example.com/mcp",
        client_metadata=metadata,
        storage=storage,
        redirect_handler=_noop_redirect,
        callback_handler=_noop_callback,
    )

    await provider._initialize()

    assert provider.context.token_expiry_time is not None, (
        "Fix A: _initialize must seed context.token_expiry_time so "
        "is_token_valid() correctly reports expiry on cold-load."
    )
    # Should be ~7200s in the future (fresh write).
    assert provider.context.token_expiry_time > time.time() + 7000
    assert provider.context.token_expiry_time <= time.time() + 7200 + 5

@pytest.mark.asyncio
async def test_initialize_marks_zero_ttl_cold_loaded_token_invalid(
    tmp_path, monkeypatch
):
    """An expired token must not pass the SDK's same-tick validity check.

    ``OAuthContext.update_token_expiry`` maps ``expires_in=0`` to ``time.time()``
    and ``is_token_valid`` accepts equality.  Cold-loaded expired tokens therefore
    need an expiry that is already in the past before the SDK selects its refresh
    or authorization-code path.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage, _get_token_dir
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests

    assert _HERMES_PROVIDER_CLS is not None
    reset_manager_for_tests()

    storage = HermesTokenStorage("srv")
    await storage.set_tokens(
        OAuthToken(
            access_token="expired-access",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="refresh-token",
        )
    )
    token_path = _get_token_dir() / "srv.json"
    persisted = json.loads(token_path.read_text())
    fixed_now = time.time()
    persisted["expires_at"] = fixed_now - 60
    token_path.write_text(json.dumps(persisted))
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
    )

    provider = _HERMES_PROVIDER_CLS(
        server_name="srv",
        server_url="https://example.com/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            client_name="Hermes Agent",
        ),
        storage=storage,
        redirect_handler=_noop_redirect,
        callback_handler=_noop_callback,
    )

    # Reproduce the SDK's equality boundary deterministically: its zero-TTL
    # expiry is exactly ``time.time()``, and validity accepts ``<=``.
    monkeypatch.setattr("mcp.client.auth.oauth2.time.time", lambda: fixed_now)
    await provider._initialize()

    assert provider.context.current_tokens is not None
    assert provider.context.current_tokens.expires_in == 0
    assert not provider.context.is_token_valid(), (
        "An expired cold-loaded token must be invalid before the SDK chooses "
        "between refresh and authorization-code flow."
    )

async def _noop_redirect(_url: str) -> None:
    return None

async def _noop_callback() -> tuple[str, str | None]:
    raise AssertionError("callback handler should not be invoked in these tests")

# ---------------------------------------------------------------------------
# Pre-flight OAuth metadata discovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_prefetches_oauth_metadata_when_missing(
    tmp_path, monkeypatch
):
    """Cold-load must pre-flight PRM + ASM discovery so ``_refresh_token``
    has the correct ``token_endpoint`` before the first refresh attempt.

    Without this, the SDK's ``_refresh_token`` falls back to
    ``{server_url}/token`` which is wrong for providers whose AS is at
    a different origin. BetterStack specifically: MCP at
    ``mcp.betterstack.com`` but token_endpoint at
    ``betterstack.com/oauth/token``. Without pre-flight the refresh 404s
    and we drop into full browser re-auth — visible to the user as an
    unwanted OAuth browser prompt every time the process restarts.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # The SDK's httpx flavour (httpx2 on mcp >= 2.0). _prefetch_oauth_metadata
    # builds its client from the same module, so the MockTransport and the
    # patched AsyncClient below have to come from there too.
    from tools.mcp_tool import sdk_httpx
    httpx = sdk_httpx()
    from mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthToken,
    )
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests

    assert _HERMES_PROVIDER_CLS is not None
    reset_manager_for_tests()

    storage = HermesTokenStorage("srv")
    await storage.set_tokens(
        OAuthToken(
            access_token="a",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="r",
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
    )

    # Route the AsyncClient used inside _prefetch_oauth_metadata through a
    # MockTransport that mimics BetterStack's split-origin discovery:
    #   PRM at mcp.example.com/.well-known/oauth-protected-resource -> points to auth.example.com
    #   ASM at auth.example.com/.well-known/oauth-authorization-server -> token_endpoint at auth.example.com/oauth/token
    seen_user_agents: list = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen_user_agents.append(request.headers.get("User-Agent"))
        if url.endswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.example.com",
                    "authorization_servers": ["https://auth.example.com"],
                    "scopes_supported": ["read", "write"],
                    "bearer_methods_supported": ["header"],
                },
            )
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.com",
                    "authorization_endpoint": "https://auth.example.com/oauth/authorize",
                    "token_endpoint": "https://auth.example.com/oauth/token",
                    "registration_endpoint": "https://auth.example.com/oauth/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "scopes_supported": ["read", "write"],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    # Patch the AsyncClient constructor used by _prefetch_oauth_metadata so
    # it uses our mock transport instead of the real network.
    real_httpx = httpx

    original_async_client = real_httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(real_httpx, "AsyncClient", patched_async_client)

    metadata = OAuthClientMetadata(
        redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
        client_name="Hermes Agent",
    )
    provider = _HERMES_PROVIDER_CLS(
        server_name="srv",
        server_url="https://mcp.example.com",
        client_metadata=metadata,
        storage=storage,
        redirect_handler=_noop_redirect,
        callback_handler=_noop_callback,
    )

    await provider._initialize()

    assert provider.context.protected_resource_metadata is not None, (
        "Pre-flight must cache PRM for the SDK to reference later."
    )
    assert provider.context.oauth_metadata is not None, (
        "Pre-flight must cache ASM so _refresh_token builds the correct "
        "token_endpoint URL."
    )
    assert str(provider.context.oauth_metadata.token_endpoint) == (
        "https://auth.example.com/oauth/token"
    )
    # Pre-flight discovery requests are SDK-built (no client default headers merged),
    # so they must carry the stamped User-Agent or WAF-fronted providers 403 them (#113771).
    from tools.mcp_oauth_provider import DEFAULT_AUTH_REQUEST_USER_AGENT
    assert seen_user_agents and set(seen_user_agents) == {DEFAULT_AUTH_REQUEST_USER_AGENT}


# ---------------------------------------------------------------------------
# Expiry margin: refresh shortly before expiry, not at the exact second
# ---------------------------------------------------------------------------

def _provider_for(storage):
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests

    reset_manager_for_tests()
    return _HERMES_PROVIDER_CLS(
        server_name="srv",
        server_url="https://example.com/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            client_name="Hermes Agent",
        ),
        storage=storage,
        redirect_handler=_noop_redirect,
        callback_handler=_noop_callback,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "issued, remaining, refresh, expect_valid",
    [
        (3600, 2, "r", False),   # reloaded seconds before expiry: refresh first, never send it
        (3600, 300, "r", True),  # outside the margin: keep using it
        (30, 20, "r", True),     # short-lived token: margin capped at a tenth of its lifetime
        (3600, 2, None, True),   # no refresh token: nothing to refresh with, use it until expiry
    ],
)
async def test_token_near_expiry_counts_as_expired(tmp_path, monkeypatch, issued, remaining, refresh, expect_valid):
    """A token in its last seconds must be refreshed before the request, not sent.

    The SDK accepts a token until ``time.time() <= expiry`` and answers the
    resulting 401 with a full browser login instead of a refresh, so a request
    racing the expiry boundary ends in ``OAuthNonInteractiveError`` for
    background callers. Covers both the cold-load path (``expires_in`` is the
    remaining time) and a fresh token response (``expires_in`` is the lifetime).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from mcp.shared.auth import OAuthToken

    from tools.mcp_oauth import HermesTokenStorage, _get_token_dir

    storage = HermesTokenStorage("srv")
    await storage.set_tokens(OAuthToken(access_token="a", expires_in=issued, refresh_token=refresh))
    token_path = _get_token_dir() / "srv.json"
    persisted = json.loads(token_path.read_text())
    persisted["expires_at"] = time.time() + remaining
    token_path.write_text(json.dumps(persisted))

    cold = _provider_for(storage)
    await cold._initialize()
    assert cold.context.is_token_valid() is expect_valid

    fresh = _provider_for(HermesTokenStorage("srv"))
    fresh.context.current_tokens = OAuthToken(access_token="b", expires_in=issued, refresh_token=refresh)
    fresh.context.update_token_expiry(fresh.context.current_tokens)
    margin = min(60, issued // 10) if refresh else 0
    assert fresh.context.token_expiry_time == pytest.approx(time.time() + issued - margin, abs=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, body, keeps_refresh_token",
    [
        (503, b"<html>down</html>", True),            # endpoint outage: grant kept for the refresh at expiry
        (400, b'{"error":"invalid_grant"}', False),  # rejected grant: only the access token is kept
    ],
)
async def test_failed_early_refresh_still_sends_live_access_token(tmp_path, monkeypatch, status, body, keeps_refresh_token):
    """A refresh started inside the margin that fails must not cost the token's last seconds.

    The SDK adds the Bearer header only when ``is_token_valid()``; without a
    fallback the request would go out bare, get 401 and start a browser login,
    where the same request without the margin succeeds with the live token.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
    from pydantic import AnyUrl

    import tools.mcp_oauth_provider as provider_mod
    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_tool import sdk_httpx

    async def _no_wait(_delay):
        return None

    monkeypatch.setattr(provider_mod, "_sleep", _no_wait)
    httpx = sdk_httpx()
    storage = HermesTokenStorage("srv")
    await storage.set_tokens(OAuthToken(access_token="live", expires_in=3600, refresh_token="r"))
    provider = _provider_for(storage)
    provider._initialized = True
    provider.context.client_info = OAuthClientInformationFull(
        client_id="cid", redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")])
    provider.context.current_tokens = OAuthToken(access_token="live", expires_in=40, refresh_token="r")
    provider.context.update_token_expiry(provider.context.current_tokens)

    request = httpx.Request("POST", "https://example.com/mcp")
    flow = provider.async_auth_flow(request)
    out = await flow.asend(None)
    refresh_posts = 0
    while out is not request:
        refresh_posts += 1
        out = await flow.asend(httpx.Response(status, content=body, request=out))
    assert refresh_posts >= 1
    assert out.headers.get("Authorization") == "Bearer live"
    assert bool(provider.context.current_tokens.refresh_token) is keeps_refresh_token
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(200, request=request))
