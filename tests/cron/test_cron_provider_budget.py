"""A provider's exhausted credits fail the cron run with type ``budget``, never ``ok`` or ``auth``.

Field case: the Dan Shipper X monitor's ``x_search`` got xAI's 403
``personal-team-blocked:spending-limit``. The agent wrote a "could not check" report and the run
was recorded ``ok``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import cron.incidents as incidents
import cron.scheduler as scheduler
from cron.provider_budget import (
    BUDGET_FAILURE_TYPE, TOOL_BUDGET_ERROR_PREFIX, is_provider_budget_error, tool_budget_failure)
from cron.scheduler_failure_copy import classify_cron_failure_reason

# Verbatim x_search result from the field run (tools/x_search_tool.py::_error_json).
XAI_SPENDING_LIMIT_RESULT = json.dumps({
    "success": False, "provider": "xai", "tool": "x_search",
    "error": (
        "personal-team-blocked:spending-limit: You have run out of credits or need a Grok "
        "subscription. Add credits at https://grok.com/?_s=usage or upgrade at "
        "https://grok.com/supergrok."
    ),
    "error_type": "HTTPError",
})
OPENAI_INSUFFICIENT_QUOTA = (
    "Error code: 429 - {'error': {'message': 'You exceeded your current quota, please check your "
    "plan and billing details.', 'type': 'insufficient_quota', 'code': 'insufficient_quota'}}"
)
JOB = {"name": "Dan Shipper X monitor", "id": "baff7141c0de"}


def _tool(name, content):
    return {"role": "tool", "name": name, "tool_name": name, "tool_call_id": f"call_{name}",
            "content": content}


# ── Matcher ────────────────────────────────────────────────────────────────


def test_budget_markers_are_provider_wording_not_generic_limits():
    for text in (
        "personal-team-blocked:spending-limit: You have run out of credits",
        OPENAI_INSUFFICIENT_QUOTA,
        "Your credit balance is too low to access the Anthropic API.",
        "HTTP 402: Insufficient credits. Add more using https://openrouter.ai/settings/credits",
        "403 Budget limit exceeded (monthly limit)",
    ):
        assert is_provider_budget_error(text), text
    for text in (
        "HTTP 429: rate limit exceeded",
        "You have hit your weekly usage limit",
        "Your quota will reset when the current 7-day window ends",
        "Error code: 401 - invalid api key",
        "credits: Dan Shipper thanked the team",
    ):
        assert not is_provider_budget_error(text), text


# ── Tool-result evidence ───────────────────────────────────────────────────


def test_xai_spending_limit_tool_result_is_a_budget_failure():
    error = tool_budget_failure([
        {"role": "user", "content": "check @danshipper"},
        _tool("x_search", XAI_SPENDING_LIMIT_RESULT),
        _tool("web_search", json.dumps({"success": True, "data": {"web": []}})),
    ])
    assert error is not None
    assert error.startswith(f"{TOOL_BUDGET_ERROR_PREFIX} x_search (xai): ")
    assert "personal-team-blocked:spending-limit" in error


def test_tool_error_from_registry_helper_counts():
    """``tools.registry.tool_error`` results carry no ``success`` key; their ``error`` counts."""
    error = tool_budget_failure([_tool("image_generate", json.dumps({"error": OPENAI_INSUFFICIENT_QUOTA}))])
    assert error is not None and error.startswith(f"{TOOL_BUDGET_ERROR_PREFIX} image_generate: ")


def test_non_evidence_tool_results_do_not_fail_the_run():
    quoted = "Thread: 'I have run out of credits on xAI again' says a user."
    assert tool_budget_failure([
        # Successful results that merely mention credits.
        _tool("x_search", json.dumps({"success": True, "answer": quoted})),
        _tool("terminal", json.dumps({"output": "insufficient_quota", "exit_code": 1, "error": None})),
        # Third-party content inside the untrusted wrapper, even shaped as an error.
        _tool("web_extract", '<untrusted_tool_result source="web_extract">\n'
              + json.dumps({"error": "insufficient_quota"}) + "\n</untrusted_tool_result>"),
        # Plain text and non-budget errors.
        _tool("read_file", "run out of credits"),
        _tool("x_search", json.dumps({"success": False, "error": "HTTP 503: upstream unavailable"})),
        {"role": "assistant", "content": "x_search: personal-team-blocked:spending-limit"},
    ]) is None


def test_later_success_from_the_same_tool_clears_the_failure():
    succeeded = json.dumps({"success": True, "provider": "xai", "answer": "two new posts"})
    assert tool_budget_failure([_tool("x_search", XAI_SPENDING_LIMIT_RESULT), _tool("x_search", succeeded)]) is None
    # A different tool's success does not clear it, nor does a later non-budget error.
    later_error = json.dumps({"success": False, "error": "x_search timed out"})
    assert tool_budget_failure([
        _tool("x_search", XAI_SPENDING_LIMIT_RESULT), _tool("web_search", succeeded),
        _tool("x_search", later_error),
    ]) is not None


# ── Classification ─────────────────────────────────────────────────────────


def test_incident_type_is_budget_not_auth_rate_limit_or_script(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    tool_error = tool_budget_failure([_tool("x_search", XAI_SPENDING_LIMIT_RESULT)])
    assert tool_error is not None
    for error in (
        tool_error,
        # Before: "script" (the "subscription" substring).
        "RuntimeError: HTTP 403: personal-team-blocked:spending-limit: You have run out of credits "
        "or need a Grok subscription.",
        # Before: "rate_limit" (the "quota" substring).
        OPENAI_INSUFFICIENT_QUOTA,
    ):
        assert incidents._classify_failure_type(error) == BUDGET_FAILURE_TYPE, error
    # Neighbouring classes keep their types.
    assert incidents._classify_failure_type("HTTP 429: rate limit exceeded") == "rate_limit"
    assert incidents._classify_failure_type("Error code: 401 - unauthorized") == "auth"


def test_xai_spending_limit_403_is_billing_not_auth():
    """The shared classifier reads a 403 as ``auth`` and the notice said "sign in again"."""
    text = ("RuntimeError: HTTP 403: personal-team-blocked:spending-limit: You have run out of "
            "credits or need a Grok subscription.")
    assert classify_cron_failure_reason(text) == "billing"
    assert classify_cron_failure_reason("HTTP 403: Forbidden") != "billing"


def test_tool_budget_notice_names_the_tool_and_the_fix(monkeypatch):
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(scheduler, "get_fallback_chain", lambda cfg: [])
    error = tool_budget_failure([_tool("x_search", XAI_SPENDING_LIMIT_RESULT)])
    assert error is not None
    msg = scheduler._summarize_cron_failure_for_delivery(JOB, error)
    assert "x_search (xai)" in msg and "out of credits" in msg, msg
    assert "`hermes cron run baff7141c0de`" in msg, msg
    assert "/login" not in msg and "sign in" not in msg.lower(), msg


# ── run_job ────────────────────────────────────────────────────────────────


def _run_job_with_messages(tmp_path, messages, final_response):
    fake_db = MagicMock()
    fake_db.get_compression_tip.side_effect = lambda session_id: session_id
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=fake_db), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
             "api_key": "test-key", "base_url": "https://example.invalid/v1",
             "provider": "openrouter", "api_mode": "chat_completions"}), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {
            "final_response": final_response, "messages": messages}
        mock_agent_cls.return_value = mock_agent
        return scheduler.run_job({"id": "baff7141c0de", "name": "X monitor", "prompt": "check X"})


def test_run_job_fails_when_a_tool_provider_is_out_of_credits(tmp_path):
    report = "The check is incomplete: x_search is blocked by the xAI spending limit."
    success, output, final_response, error = _run_job_with_messages(
        tmp_path, [_tool("x_search", XAI_SPENDING_LIMIT_RESULT)], report)

    assert success is False
    assert error.startswith(TOOL_BUDGET_ERROR_PREFIX) and "spending-limit" in error
    assert final_response == report
    assert "(FAILED)" in output and "## Failure" in output
    # The agent's report stays the archived answer that later runs read.
    assert output.rpartition("## Response")[2].strip() == report


def test_run_job_stays_ok_without_budget_evidence(tmp_path):
    success, _output, final_response, error = _run_job_with_messages(
        tmp_path, [_tool("x_search", json.dumps({"success": True, "answer": "two posts"}))], "two posts")
    assert (success, error, final_response) == (True, None, "two posts")
