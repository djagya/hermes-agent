"""Guardian outcome typing in tools/approval_smart.py.

Contract: APPROVE/DENY are decisions; every non-decision (blank content,
truncated or unrecognized text, provider exception, an explicit ESCALATE)
returns ``escalate`` AND records its own outcome category with finish reason
and a redacted, bounded snippet. Escalation is a guardian-evaluation outcome,
never reported as the user's decision. Redaction must prevent secret leakage
into the observation log. No helper evaluation happens during classification
and no network call is made: the LLM boundary is monkeypatched or bypassed.
"""

from __future__ import annotations

import logging

import pytest

import tools.approval_smart as approval_smart
from agent.redact import redact_sensitive_text

SECRET = "sk-live-abcdefABCDEF12345678901234"


def _parse(content, finish_reason=""):
    return approval_smart._parse_guardian_answer(content, finish_reason, 0.01)


@pytest.fixture
def observations(caplog):
    caplog.set_level(logging.DEBUG, logger="tools.approval")
    return caplog


class TestVerdictMapping:
    def test_approve(self):
        assert _parse("APPROVE") == "approve"

    def test_deny(self):
        assert _parse("DENY") == "deny"

    def test_trailing_punctuation_is_a_formatting_artifact(self):
        assert _parse("APPROVE.") == "approve"

    def test_blank_content_escalates(self):
        assert _parse("") == "escalate"

    def test_whitespace_only_content_escalates(self):
        assert _parse("  \n ") == "escalate"

    def test_truncated_essay_escalates(self):
        assert _parse("The command appears to be safe because the script only") == "escalate"

    def test_unrecognized_word_escalates(self):
        assert _parse("MAYBE") == "escalate"

    def test_explicit_escalate_word_escalates(self):
        assert _parse("ESCALATE") == "escalate"


class TestOutcomeCategories:
    def test_blank_records_empty_response(self, observations):
        _parse("")
        assert "empty_response" in observations.text

    def test_unrecognized_records_category(self, observations):
        _parse("No single verdict here")
        assert "unrecognized_response" in observations.text

    def test_length_limited_records_category(self, observations):
        _parse("APPROV", finish_reason="length")
        assert "length_limited_response" in observations.text

    def test_explicit_escalate_records_uncertain(self, observations):
        _parse("ESCALATE")
        assert "'uncertain'" in observations.text

    def test_provider_exception_records_category(self, observations, monkeypatch):
        import agent.auxiliary_client as aux

        def boom(**_kwargs):
            raise RuntimeError("provider down")

        monkeypatch.setattr(aux, "call_llm", boom)
        monkeypatch.setattr(aux, "_get_task_timeout", lambda _task: 5)
        verdict = approval_smart._smart_approve("ls -la", "test")
        assert verdict == "escalate"
        assert "provider_exception" in observations.text


class TestObservabilityHygiene:
    def test_redaction_prevents_secret_leak_in_observation_log(self, observations):
        _parse(f"Bearer {SECRET} then more text")
        assert SECRET not in observations.text

    def test_decisions_log_at_debug_not_warning(self, caplog):
        caplog.set_level(logging.DEBUG, logger="tools.approval")
        _parse("APPROVE")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings

    def test_max_tokens_leaves_room_for_one_word(self):
        assert approval_smart._VERDICT_MAX_TOKENS >= 16
