"""Re-scope pinned request overrides when fallback changes the active route."""

from __future__ import annotations

from typing import Any


def rescope_request_overrides(
    agent: Any, *, old_model: str, old_provider: str, old_base_url: str, old_api_mode: str,
) -> None:
    """Rebuild route-owned fields, preserving unrelated caller overrides.

    Wire kwargs are already rebuilt per attempt, but static /fast parameters and
    custom-provider extra_body live on the agent and otherwise survive that rebuild.
    Credential rotation never enters this path.
    """
    from agent.agent_init import _custom_provider_extra_body_for_agent
    from hermes_cli.models import resolve_fast_mode_overrides

    overrides = dict(getattr(agent, "request_overrides", None) or {})
    custom_providers = getattr(agent, "_custom_providers", None) or []
    old_extra = _custom_provider_extra_body_for_agent(
        provider=old_provider, model=old_model, base_url=old_base_url,
        custom_providers=custom_providers,
    ) or {}
    extra = overrides.get("extra_body")
    if isinstance(extra, dict):
        # Retain the established caller-wins contract for custom-provider keys.
        extra = {k: v for k, v in extra.items() if k not in old_extra or v != old_extra[k]}
        overrides["extra_body"] = extra

    # Re-resolve fast fields even for a same-provider fallback to another model.
    # Bounded auto/cold windows are still layered by effective_request_overrides.
    for key in ("speed", "service_tier"):
        overrides.pop(key, None)
        if isinstance(extra, dict):
            extra.pop(key, None)

    route_changed = (old_provider, old_base_url, old_api_mode) != (
        agent.provider, agent.base_url, agent.api_mode,
    )
    if route_changed:
        overrides.pop("betas", None)
        if isinstance(extra, dict):
            extra.pop("betas", None)
        headers = overrides.get("extra_headers")
        if isinstance(headers, dict):
            headers = {k: v for k, v in headers.items() if not k.lower().startswith("anthropic-")}
            if headers:
                overrides["extra_headers"] = headers
            else:
                overrides.pop("extra_headers", None)

    new_extra = _custom_provider_extra_body_for_agent(
        provider=agent.provider, model=agent.model, base_url=agent.base_url,
        custom_providers=custom_providers,
    ) or {}
    if new_extra or isinstance(extra, dict):
        merged = {**new_extra, **(extra if isinstance(extra, dict) else {})}
        if merged:
            overrides["extra_body"] = merged
        else:
            overrides.pop("extra_body", None)

    if getattr(agent, "service_tier", None) == "priority":
        base_url = agent.base_url
        if agent.api_mode == "anthropic_messages":
            base_url = getattr(agent, "_anthropic_base_url", None) or base_url
        overrides.update(resolve_fast_mode_overrides(
            agent.model, provider=agent.provider, base_url=base_url,
        ) or {})
    agent.request_overrides = overrides
