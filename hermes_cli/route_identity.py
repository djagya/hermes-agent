"""Fail-closed URL identity normalization for model/provider routes."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit


def normalize_route_base_url(base_url: Any) -> str:
    """Canonicalize only proven-equivalent endpoint URL components."""
    raw = str(base_url or "")
    if not raw:
        return ""
    if any(ord(char) <= 0x20 for char in raw):
        return raw
    had_query_delimiter = "?" in raw.split("#", 1)[0]
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return raw
        scheme = parsed.scheme.lower()
        if "%" in hostname:
            address, zone = hostname.split("%", 1)
            host = f"{address.lower()}%{zone}"
        else:
            host = hostname.lower()
        port = parsed.port
    except (TypeError, ValueError):
        return raw
    route_host = parsed.netloc.rsplit("@", 1)[-1]
    if route_host.startswith("[") or ":" in host:
        host = f"[{host}]"
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    if "@" in parsed.netloc:
        host = f"{parsed.netloc.rsplit('@', 1)[0]}@{host}"
    path = parsed.path
    if path.endswith("/") and not had_query_delimiter:
        path = path[:-1]
    normalized = urlunsplit((scheme, host, path, parsed.query, ""))
    if had_query_delimiter and not parsed.query:
        normalized += "?"
    return normalized


# xai-oauth has two spellings of ONE route: the legacy API URL that stored state keeps (auth.json,
# the pool, config.yaml, session rows; an image rolled back still reads it) and the subscription proxy
# the runtime uses. ``canonical_xai_route`` is the read side (every comparison), ``persisted_xai_route``
# the write side (every persisted base_url). Only these exact two URLs alias, only for xai-oauth: the
# API-key provider ``xai`` keeps api.x.ai as its own route, and a custom ``*.x.ai`` host is its own route.
# ``tests/hermes_cli/test_xai_route_identity_sites.py`` enumerates every site that must use them.
XAI_OAUTH_PROVIDER_IDS = frozenset({"xai-oauth", "grok-oauth", "x-ai-oauth", "xai-grok-oauth"})


def _xai_oauth_route_pair() -> tuple[str, str]:
    from hermes_cli.auth_constants import DEFAULT_XAI_OAUTH_BASE_URL, LEGACY_XAI_OAUTH_BASE_URL
    return DEFAULT_XAI_OAUTH_BASE_URL, LEGACY_XAI_OAUTH_BASE_URL


def is_xai_oauth_provider(provider: Any) -> bool:
    return str(provider or "").strip().lower() in XAI_OAUTH_PROVIDER_IDS


def canonical_xai_route(provider: Any, base_url: Any) -> str:
    """The live spelling of ``base_url`` for comparison: for xai-oauth the stored legacy API URL is the
    subscription proxy route; any other URL or provider is returned unchanged (as ``str``)."""
    raw = "" if base_url is None else str(base_url)
    if is_xai_oauth_provider(provider):
        live, legacy = _xai_oauth_route_pair()
        if normalize_route_base_url(raw.strip()) == legacy:
            return live
    return raw


def persisted_xai_route(provider: Any, base_url: Any) -> str:
    """The spelling of ``base_url`` to store: for xai-oauth the subscription proxy route is stored as the
    legacy API URL (rollback-safe; readers map it back); any other URL or provider is returned unchanged."""
    raw = "" if base_url is None else str(base_url)
    if is_xai_oauth_provider(provider):
        live, legacy = _xai_oauth_route_pair()
        if normalize_route_base_url(raw.strip()) == live:
            return legacy
    return raw


def persisted_route_patch(patch: dict) -> dict:
    """A session ``model_config`` patch with its route ``base_url`` (top level and nested ``gateway_runtime``)
    in the stored spelling; ``SessionDB.session_gateway_runtime`` reads it back in the live spelling."""
    out = dict(patch)
    if out.get("base_url"):
        out["base_url"] = persisted_xai_route(out.get("provider"), out["base_url"])
    runtime = out.get("gateway_runtime")
    if isinstance(runtime, dict) and runtime.get("base_url"):
        out["gateway_runtime"] = {**runtime, "base_url": persisted_xai_route(runtime.get("provider"), runtime["base_url"])}
    return out


def route_key(provider: Any, base_url: Any) -> str:
    """Route-identity key of ``(provider, base_url)``: compare two routes with ``route_key(p1, u1) ==
    route_key(p2, u2)``, never with raw or merely normalized URLs (the xai-oauth spellings differ).
    Transport decisions (which URL a client dials, cache keys) keep the raw URL."""
    return normalize_route_base_url(canonical_xai_route(provider, base_url))


def provider_owns_route(provider: Any, base_url: Any, config: Any = None) -> Optional[bool]:
    """Whether ``model.base_url`` is *provider*'s own endpoint.

    ``True``: the host is the provider's registry/plugin endpoint or the endpoint of a
    ``providers:``/``custom_providers:`` entry that resolves to *provider*, or *provider* is bare
    ``custom``/``local`` (the URL *is* the route). ``False``: the host is another known provider's
    stock endpoint, or *provider* names a user entry with a different endpoint. ``None``: unknown
    host (a proxy, a LAN server) — nothing here can say whose it is. Offline: the registry lookup
    never fetches the models.dev catalog.
    """
    from hermes_cli.providers import get_provider, normalize_provider, resolve_custom_provider, resolve_user_provider
    from utils import base_url_hostname

    host = base_url_hostname(str(base_url or ""))
    if not host:
        return None
    raw = str(provider or "").strip().lower()
    canonical = normalize_provider(raw)
    if canonical in ("custom", "local"):
        # Bare custom / local-server aliases are configured BY model.base_url (#14676, #27132);
        # resolve_custom_provider would also self-heal bare "custom" to the first entry (#17478).
        return True
    cfg = config if isinstance(config, dict) else {}
    user_pdef = resolve_user_provider(raw, cfg.get("providers") or {}) or resolve_custom_provider(
        raw, cfg.get("custom_providers"))
    if user_pdef is not None and user_pdef.base_url:
        return base_url_hostname(user_pdef.base_url) == host
    pdef = get_provider(canonical, allow_network=False)
    if pdef is not None and pdef.base_url and base_url_hostname(pdef.base_url) == host:
        return True
    if pdef is None and user_pdef is None:
        return True
    from agent.model_metadata import _infer_provider_from_url
    inferred = _infer_provider_from_url(str(base_url))
    if inferred is None:
        return None
    return normalize_provider(inferred) == canonical


def drop_stale_model_route(model_cfg: Any, provider: Any, config: Any = None) -> tuple[dict[str, Any], bool]:
    """Pop the route keys (``base_url``, ``api_mode``) a previous provider left in ``model:``
    when the block is re-pointed at *provider* without a fresh route (``hermes config set
    model.provider``). Mirrors what a persisted ``/model`` switch writes: the route is synced to
    the target, never carried over. Returns ``(popped {key: old value}, unverified)`` where
    ``unverified`` is True when a base_url of unknown ownership was kept — the caller should say so.
    A base_url that belongs to *provider* is kept together with its api_mode; with no base_url the
    api_mode alone is wire state of the old route and goes."""
    if not isinstance(model_cfg, dict):
        return {}, False
    base_url = str(model_cfg.get("base_url") or "").strip()
    owned = provider_owns_route(provider, base_url, config) if base_url else False
    if owned:
        return {}, False
    if owned is None:
        return {}, True
    popped = {k: model_cfg.pop(k) for k in ("base_url", "api_mode") if model_cfg.get(k) not in (None, "")}
    return popped, False


def should_clear_context_pin(configured_model: Any, active_model: Any, configured_base_url: Any, active_base_url: Any,
                             configured_provider: Any, active_provider: Any) -> bool:
    """True when a configured ``model.context_length`` pin no longer matches its runtime route.
    Fail-closed: any error during route comparison returns ``True`` (drop the pin) so a stale window
    never silently inflates the compression threshold."""
    configured_model = str(configured_model or "").strip()
    if configured_model and configured_model != str(active_model or "").strip():
        return True
    try:
        from agent.agent_init import _context_route_mismatch
        return _context_route_mismatch(configured_base_url, active_base_url, configured_provider, active_provider)
    except Exception:
        return True


async def should_clear_context_pin_async(*args: Any) -> bool:
    """``should_clear_context_pin`` on a worker thread so async gateway handlers never run it on the
    event loop — the resolution chain is cache-only (``allow_network=False``) but can still do
    cold-start disk I/O."""
    import asyncio
    return await asyncio.to_thread(should_clear_context_pin, *args)
