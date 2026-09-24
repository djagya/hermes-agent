"""HER-193: execute_code safe-operation path.

Contracts pinned here:
  - a grammar cell in a clean-lineage session skips the gate, and every non-grammar cell
    taints that session so later grammar cells go back through the normal gate;
  - the whole-script gate is untouched for non-grammar cells and when ``safe_path`` is off;
  - the session kernel re-checks lineage itself and refuses a safe-path admission once it
    has run any non-grammar cell (a racing parallel call cannot slip through).
"""

from __future__ import annotations

import json
import sys
import tempfile

import pytest

from tools import approval as A
from tools import approval_context
from tools import approval_safe_path as SP

SAFE_CELL = "from hermes_tools import read_file\nprint(read_file('notes.txt').get('content', '')[:80])"
UNSAFE_CELL = "import os\nprint(os.getcwd())"


@pytest.fixture
def gw_session(monkeypatch):
    """Manual-mode gateway session with no notify callback: a gated cell comes back
    ``pending_approval``, so an auto-approval can only come from the safe path."""
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    for var in ("HERMES_INTERACTIVE", "HERMES_CRON_SESSION", "HERMES_EXEC_ASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    config = {"mode": "manual", "safe_path": True}
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: config)
    session_key = "safe-path-session"
    token = approval_context.set_current_session_key(session_key)
    SP.forget_session(session_key)
    with A._lock:
        A._gateway_notify_cbs.pop(session_key, None)
        A._permanent_approved.discard("execute_code")
        A._session_approved.get(session_key, set()).discard("execute_code")
    try:
        yield config
    finally:
        approval_context.reset_current_session_key(token)
        SP.forget_session(session_key)
        with A._lock:
            A._gateway_queues.pop(session_key, None)


def test_grammar_cell_skips_gate_until_lineage_is_tainted(gw_session):
    first = A.check_execute_code_guard(SAFE_CELL, "local")
    assert first["approved"] is True
    assert first["decision_source"] == "safe_path"

    gated = A.check_execute_code_guard(UNSAFE_CELL, "local")
    assert gated["approved"] is False
    assert gated.get("status") == "pending_approval"

    # Same safe cell, but the kernel namespace may now be poisoned: back to the gate.
    after = A.check_execute_code_guard(SAFE_CELL, "local")
    assert after["approved"] is False
    assert after.get("decision_source") != "safe_path"


def test_operator_off_switch_and_non_local_backends_keep_the_gate(gw_session):
    gw_session["safe_path"] = "false"
    assert A.check_execute_code_guard(SAFE_CELL, "local")["approved"] is False
    gw_session["safe_path"] = True
    # The kernel-side lineage re-check exists only on the local session kernel.
    remote = A.check_execute_code_guard(SAFE_CELL, "ssh")
    assert remote.get("decision_source") != "safe_path"


def test_kernel_refuses_safe_admission_after_a_non_grammar_cell(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    from tools.code_kernel import execute_in_session_kernel, shutdown_all_kernels

    def run(code, admitted):
        return json.loads(execute_in_session_kernel(
            code, task_id="safe-path-kernel", mode="strict", child_python=sys.executable,
            child_cwd=tempfile.gettempdir(), sandbox_tools=frozenset({"read_file"}),
            timeout=30, max_tool_calls=5, reset=False, is_interrupted=lambda: False,
            safe_path_admitted=admitted))

    shutdown_all_kernels()
    try:
        assert run("print(40 + 2)", True)["status"] == "success"
        # A gated (non-grammar) cell rebinds a builtin the grammar allows.
        assert run("len = lambda x: 0", False)["status"] == "success"
        refused = run("print(len([1, 2]))", True)
        assert refused["status"] == "error"
        assert refused["outcome"] == "lineage_tainted"
        # Through the normal gate the kernel still runs it (with the poisoned name).
        assert run("print(len([1, 2]))", False)["output"].strip() == "0"
    finally:
        shutdown_all_kernels()
