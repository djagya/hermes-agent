"""Tests for the /refine focus parameter on spawn_background_review_thread."""

from unittest.mock import MagicMock, patch

import pytest

from agent.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _MEMORY_REVIEW_PROMPT,
    spawn_background_review_thread,
)


def _bare_agent():
    agent = MagicMock()
    # Ensure getattr(agent, "_COMBINED_REVIEW_PROMPT", default) hits the default.
    del agent._COMBINED_REVIEW_PROMPT
    del agent._MEMORY_REVIEW_PROMPT
    del agent._SKILL_REVIEW_PROMPT
    return agent


def test_no_focus_prompt_is_byte_identical():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True
    )
    assert prompt == _COMBINED_REVIEW_PROMPT

    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True, focus=None
    )
    assert prompt == _COMBINED_REVIEW_PROMPT

    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True, focus="   "
    )
    assert prompt == _COMBINED_REVIEW_PROMPT


def test_focus_is_appended_to_prompt():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True,
        focus="save the deploy workflow as a skill",
    )
    assert prompt.startswith(_COMBINED_REVIEW_PROMPT)
    assert "save the deploy workflow as a skill" in prompt


def test_focus_works_with_memory_only_prompt():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=False, focus="remember my timezone",
    )
    assert prompt.startswith(_MEMORY_REVIEW_PROMPT)
    assert "remember my timezone" in prompt


@pytest.mark.parametrize("explicit", [False, True])
def test_refine_runs_on_main_model_while_automatic_review_keeps_route(explicit):
    """/refine (explicit) ignores the auxiliary.background_review model route and runs on the
    parent runtime with the full snapshot; the automatic review keeps the configured route."""
    from agent import background_review as br

    route = {"provider": "openai-codex", "model": "cheap-model", "reasoning_effort": "low",
             "max_input_tokens": 1234}
    captured = {}

    def fake_worker(agent, messages_snapshot, prompt, task_cfg=None, **_kw):
        captured["task_cfg"] = task_cfg

    agent = _bare_agent()
    agent.provider, agent.model = "openrouter", "main-model"
    agent._current_main_runtime.return_value = {"api_mode": "chat_completions"}
    with patch.object(br, "_run_review_in_thread", fake_worker):
        target, _prompt = spawn_background_review_thread(
            agent, [], review_memory=True, task_cfg=route, explicit=explicit)
        target()
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={"provider": "openai-codex", "model": "cheap-model"}):
        rt = br._resolve_review_runtime(agent, captured["task_cfg"])
    assert rt["routed"] is not explicit
    assert rt["model"] == ("main-model" if explicit else "cheap-model")
    assert captured["task_cfg"]["max_input_tokens"] == 1234  # non-route keys still apply
    assert route["model"] == "cheap-model"  # caller's block is not mutated
