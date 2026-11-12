"""The Codex backend rejects a replayed sol-minted encrypted reasoning item with a bare
``{"detail": "Bad Request"}`` 400. It maps to ``invalid_encrypted_content``: the first rejection
strips the cached reasoning and retries the same model; a repeat in the turn falls back; a history
with no cached reasoning behaves as before (no retry, straight to the fallback chain)."""
from __future__ import annotations

import logging

from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_recovery import recover_after_classification
from agent.turn_retry_state import TurnRetryState


class _Resp:
    headers = {"x-request-id": "req_abc123", "cf-ray": "9abc-FRA"}


class _Codex400(Exception):
    def __init__(self):
        super().__init__("Error code: 400 - {'detail': 'Bad Request'}")
        self.status_code = 400
        self.body = {"detail": "Bad Request"}
        self.response = _Resp()


class _Agent:
    log_prefix = ""
    provider = "openai-codex"
    api_mode = "codex_responses"
    base_url = "https://chatgpt.com/backend-api/codex"
    model = "gpt-5.5"
    _codex_reasoning_replay_enabled = True
    _codex_reasoning_replay_rejected = False

    def _recover_with_credential_pool(self, **kwargs):
        return False, False

    def _extract_api_error_context(self, error):
        from agent.agent_runtime_helpers import extract_api_error_context
        return extract_api_error_context(error)

    def _disable_codex_reasoning_replay(self, messages=None, **kwargs):
        from run_agent import AIAgent
        return AIAgent._disable_codex_reasoning_replay(self, messages, **kwargs)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _sol_history(n: int = 2):
    return [
        {"role": "assistant", "content": f"answer {i}",
         "codex_reasoning_items": [{"type": "reasoning", "id": f"rs_{i}", "encrypted_content": "gAAA" * 8,
                                    "_issuer_kind": "chatgpt_codex", "_issuer_model": "gpt-5.5-sol"}]}
        for i in range(n)
    ]


def _recover(agent, err, retry, messages):
    classified = classify_api_error(err, provider=agent.provider, model=agent.model)
    assert classified.reason == FailoverReason.invalid_encrypted_content
    assert classified.retryable is False and classified.should_fallback is True
    return recover_after_classification(
        agent, err, classified, retry, status_code=400,
        error_context=agent._extract_api_error_context(err), messages=messages, api_messages=list(messages),
    )


def test_bad_request_strips_cached_reasoning_once_then_falls_back():
    agent, retry, messages = _Agent(), TurnRetryState(), _sol_history()

    assert _recover(agent, _Codex400(), retry, messages) == (True, False)  # strip + retry same model
    assert not any("codex_reasoning_items" in m for m in messages)
    # A second Bad Request in the same turn is not recoverable here: no loop, the caller falls back.
    assert _recover(agent, _Codex400(), retry, _sol_history()) == (False, False)


def test_bad_request_without_cached_reasoning_is_not_retried():
    agent, retry = _Agent(), TurnRetryState()
    history = [{"role": "assistant", "content": "plain"}]

    assert _recover(agent, _Codex400(), retry, history) == (False, False)
    assert retry.invalid_encrypted_content_retry_visit is None  # nothing consumed
    assert agent._codex_reasoning_replay_enabled is True
    assert history == [{"role": "assistant", "content": "plain"}]


def test_bad_request_logs_counts_and_request_ids_only(caplog):
    agent, retry, messages = _Agent(), TurnRetryState(), _sol_history(3)

    with caplog.at_level(logging.WARNING, logger="agent.turn_recovery_codex"):
        _recover(agent, _Codex400(), retry, messages)

    line = next(r.getMessage() for r in caplog.records if "Bad Request envelope" in r.getMessage())
    assert "messages=3" in line and "replayed_reasoning_items=3" in line
    assert "x-request-id=req_abc123" in line and "cf-ray=9abc-FRA" in line
    assert "gAAA" not in line and "answer" not in line


def test_bad_request_log_tolerates_missing_response_headers(caplog):
    err = _Codex400()
    err.response = None
    with caplog.at_level(logging.WARNING, logger="agent.turn_recovery_codex"):
        _recover(_Agent(), err, TurnRetryState(), [])
    line = next(r.getMessage() for r in caplog.records if "Bad Request envelope" in r.getMessage())
    assert "replayed_reasoning_items=0" in line and "x-request-id=-" in line and "cf-ray=-" in line
