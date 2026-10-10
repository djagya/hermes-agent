"""Fallback must rebuild route-owned overrides before the target's strict preflight."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import restore_primary_runtime
from agent.fast_mode import begin_turn, effective_request_overrides, mark_fast_mode_unavailable
from hermes_cli.models import resolve_fast_mode_overrides
from run_agent import AIAgent


PRIMARY_MODEL = "claude-opus-5-5"
PRIMARY_URL = "https://api.anthropic.com"
MESSAGES = [{"role": "user", "content": "Synthetic fallback request"}]


@pytest.fixture
def fast_agent(tmp_path, monkeypatch):
    # Real agent/config/transport imports; only external client construction is stubbed.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            model=PRIMARY_MODEL, provider="anthropic", api_mode="anthropic_messages",
            api_key="synthetic-primary-key", base_url=PRIMARY_URL, service_tier="priority",
            request_overrides={"speed": "fast"},
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
    return agent


@pytest.mark.parametrize("mode", ["priority", "auto", "cold"])
@pytest.mark.parametrize("provider,model,url,api_mode", [
    ("openai-codex", "gpt-6-luna", "https://chatgpt.com/backend-api/codex", "codex_responses"),
    ("openai", "gpt-5.4", "https://api.openai.com/v1", "codex_responses"),
    ("xai", "grok-4.6", "https://api.x.ai/v1", "codex_responses"),
    ("kimi-coding", "k3", "https://api.kimi.com/coding/v1", "chat_completions"),
    ("anthropic", "claude-opus-5", PRIMARY_URL, "anthropic_messages"),
    ("anthropic", "claude-sonnet-4-6", PRIMARY_URL, "anthropic_messages"),
])
def test_fallback_request_is_target_scoped(fast_agent, mode, provider, model, url, api_mode):
    agent = fast_agent
    agent.service_tier = mode
    agent.request_overrides = {
        **({"speed": "fast"} if mode == "priority" else {}),
        "betas": ["synthetic-anthropic-beta"],
        "extra_body": {"betas": ["synthetic-anthropic-beta"], "speed": "fast", "service_tier": "priority"},
        "extra_headers": {"Anthropic-Beta": "synthetic-beta", "X-Caller": "keep"},
    }
    agent._primary_runtime["request_overrides"] = deepcopy(agent.request_overrides)
    primary_overrides = deepcopy(agent.request_overrides)
    agent._fast_mode_unavailable_models.add("other-model")
    begin_turn(agent, conversation_history=[])
    primary_kwargs = agent._build_api_kwargs(MESSAGES)
    assert primary_kwargs["extra_body"]["speed"] == "fast"

    agent._fallback_chain = [{"provider": provider, "model": model, "base_url": url, "api_mode": api_mode}]
    fallback_client = MagicMock(api_key="synthetic-fallback-key", base_url=url)
    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fallback_client, model)),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        patch.object(agent, "_replace_primary_openai_client"),
    ):
        assert agent._try_activate_fallback() is True
    assert (agent.provider, agent.model, agent.api_mode) == (provider, model, api_mode)
    assert agent._fast_mode_unavailable_models == {"other-model"}
    assert agent._primary_runtime["request_overrides"] == primary_overrides

    target_fast = resolve_fast_mode_overrides(model, provider=provider, base_url=url) or {}
    effective = effective_request_overrides(agent)
    assert {k: effective[k] for k in ("speed", "service_tier") if k in effective} == target_fast
    kwargs = agent._build_api_kwargs(MESSAGES)
    if api_mode == "codex_responses":
        assert "speed" not in kwargs
        assert "speed" not in (kwargs.get("extra_body") or {})
        assert "betas" not in kwargs
        assert "betas" not in (kwargs.get("extra_body") or {})
        assert kwargs["extra_headers"] == {"X-Caller": "keep"}
        # Same adapter preflight used by the real per-attempt assembly path.
        preflight = agent._get_transport().preflight_kwargs(kwargs, allow_stream=False)
        assert preflight["model"] == model
        # Do not silently loosen the deliberate caller guard.
        with pytest.raises(ValueError, match="unsupported field.*speed"):
            agent._get_transport().preflight_kwargs({**kwargs, "speed": "fast"}, allow_stream=False)
    elif api_mode == "anthropic_messages":
        assert (kwargs.get("extra_body") or {}).get("speed") == target_fast.get("speed")
    else:
        # Kimi tolerating Anthropic's speed does not make it a legitimate Kimi override.
        assert "speed" not in kwargs
        assert "speed" not in (kwargs.get("extra_body") or {})
        assert "service_tier" not in kwargs

    if api_mode == "codex_responses":
        assert kwargs.get("service_tier") == target_fast.get("service_tier")
    with patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        assert restore_primary_runtime(agent) is True
    assert agent.request_overrides == primary_overrides
    assert agent._build_api_kwargs(MESSAGES)["extra_body"]["speed"] == "fast"


def test_same_provider_rotation_preserves_kwargs_and_capacity_learning(fast_agent):
    agent = fast_agent
    original = agent.request_overrides
    before = agent._build_api_kwargs(MESSAGES)
    entry = SimpleNamespace(
        id="second", runtime_api_key="synthetic-second-key", runtime_base_url=PRIMARY_URL,
    )
    with patch.object(agent, "_build_direct_anthropic_client", return_value=MagicMock()):
        assert agent._swap_credential(entry) is True
    assert agent.request_overrides is original
    assert agent._build_api_kwargs(MESSAGES) == before
    assert mark_fast_mode_unavailable(agent) is True
    assert mark_fast_mode_unavailable(agent) is False
    assert "speed" not in effective_request_overrides(agent)
    assert agent.request_overrides == {"speed": "fast"}
    assert "speed" not in (agent._build_api_kwargs(MESSAGES).get("extra_body") or {})
