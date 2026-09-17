"""``decision_source`` correlation on the human gate.

Contract: every result of the human approval gate — grant OR deny, on every
surface — records ``decision_source="human"`` plus the gate's ``gate_id``, so a
result log answers WHO decided without inferring it from absence-of-fields
(parity with the ``smart``/``unattended`` deny results). The prompt seam is the
real ``check_all_command_guards`` entry point with the interactive callback
contract; no guardian LLM is involved (approvals.mode=manual).
"""

from __future__ import annotations

import pytest

import tools.approval as approval_module
from tools import approval_context
from tools.approval import check_all_command_guards, clear_session
from tools.approval_context import set_current_session_key


@pytest.fixture
def isolated_session(monkeypatch):
    """Fresh session key, clean approval state, interactive CLI context."""
    session_key = "test:session:decision_source"
    token = set_current_session_key(session_key)
    monkeypatch.setenv("HERMES_SESSION_KEY", session_key)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    saved_permanent = approval_module._permanent_approved.copy()
    saved_session = {k: v.copy() for k, v in approval_module._session_approved.items()}
    approval_module._permanent_approved.clear()
    approval_module._session_approved.clear()
    try:
        yield session_key
    finally:
        approval_module._permanent_approved.update(saved_permanent)
        approval_module._session_approved.update(saved_session)
        try:
            approval_context._approval_session_key.reset(token)
        except Exception:
            pass
        clear_session(session_key)


def test_human_grant_records_decision_source_and_gate_id(isolated_session):
    def cb(command, description, *, allow_permanent=True):
        return "once"

    result = check_all_command_guards("rm -rf /tmp/test-decision-source", "local",
                                      approval_callback=cb)
    assert result["approved"] is True
    assert result["decision_source"] == "human"
    assert result["gate_id"] == "command"


def test_human_deny_records_decision_source_and_gate_id(isolated_session):
    def cb(command, description, *, allow_permanent=True):
        return "deny"

    result = check_all_command_guards("rm -rf /tmp/test-decision-source", "local",
                                      approval_callback=cb)
    assert result["approved"] is False
    assert result["decision_source"] == "human"
    assert result["gate_id"] == "command"
