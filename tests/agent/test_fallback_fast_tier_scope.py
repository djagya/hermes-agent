"""A static /fast tier pinned for the primary route must not ride into a fallback route.

The primary's ``speed: "fast"`` (Anthropic) or ``service_tier`` (OpenAI / xAI) lives in
``agent.request_overrides``. Fallback re-scoped only ``extra_body``, so the Codex Responses
preflight rejected the inherited ``speed`` on every fallback attempt.
"""

from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import restore_primary_runtime
from hermes_cli.models import resolve_fast_mode_overrides
from run_agent import AIAgent

MESSAGES = [{"role": "user", "content": "hi"}]
ANTHROPIC_URL = "https://api.anthropic.com"
CODEX_URL = "https://chatgpt.com/backend-api/codex"


def _agent(tmp_path, monkeypatch, *, provider, model, base_url, api_mode):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        return AIAgent(
            model=model, provider=provider, api_mode=api_mode, api_key="primary-key", base_url=base_url,
            service_tier="priority",
            request_overrides=resolve_fast_mode_overrides(model, provider=provider, base_url=base_url),
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )


@pytest.mark.parametrize("primary,target", [
    # Anthropic fast primary -> Codex Responses fallback (the strict preflight case).
    (("anthropic", "claude-opus-5-5", ANTHROPIC_URL, "anthropic_messages"),
     ("openai-codex", "gpt-5.4", CODEX_URL, "codex_responses")),
    # Anthropic fast primary -> chat-completions fallback that has no fast mode.
    (("anthropic", "claude-opus-5-5", ANTHROPIC_URL, "anthropic_messages"),
     ("openrouter", "moonshotai/kimi-k2", "https://openrouter.ai/api/v1", "chat_completions")),
    # Codex priority primary -> Anthropic fast-capable fallback gets its own fast field.
    (("openai-codex", "gpt-5.4", CODEX_URL, "codex_responses"),
     ("anthropic", "claude-opus-5-5", ANTHROPIC_URL, "anthropic_messages")),
])
def test_static_fast_tier_is_rescoped_on_fallback(tmp_path, monkeypatch, primary, target):
    agent = _agent(tmp_path, monkeypatch, provider=primary[0], model=primary[1], base_url=primary[2], api_mode=primary[3])
    agent.request_overrides = {**agent.request_overrides, "extra_headers": {"X-Caller": "keep"}}
    agent._primary_runtime["request_overrides"] = deepcopy(agent.request_overrides)
    primary_overrides = deepcopy(agent.request_overrides)
    assert set(primary_overrides) & {"speed", "service_tier"}

    provider, model, url, api_mode = target
    agent._fallback_chain = [{"provider": provider, "model": model, "base_url": url, "api_mode": api_mode}]
    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.auxiliary_client.resolve_provider_client",
              return_value=(MagicMock(api_key="fallback-key", base_url=url), model)),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        patch.object(agent, "_replace_primary_openai_client"),
    ):
        assert agent._try_activate_fallback() is True
    assert (agent.provider, agent.api_mode) == (provider, api_mode)

    kwargs = agent._build_api_kwargs(MESSAGES)
    if api_mode == "codex_responses":
        # The same preflight the real Responses call runs before sending.
        agent._get_transport().preflight_kwargs(kwargs, allow_stream=False)
    elif api_mode == "chat_completions":
        assert "speed" not in kwargs and "service_tier" not in kwargs
    target_fast = resolve_fast_mode_overrides(model, provider=provider, base_url=url) or {}
    pinned = {k: agent.request_overrides[k] for k in ("speed", "service_tier") if k in agent.request_overrides}
    assert pinned == target_fast
    assert agent.request_overrides["extra_headers"] == {"X-Caller": "keep"}

    with patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        assert restore_primary_runtime(agent) is True
    assert agent.request_overrides == primary_overrides
