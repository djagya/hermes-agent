"""Tests for scripts/ci/select_test_roots.py.

Behavior contracts, not snapshots: the resolver must fail closed to the full
suite whenever selection is ambiguous (never green over zero tests), always
carry the CI self-tests on selective runs, and scale slice count with the
resolved file count.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "select_test_roots.py"
_spec = importlib.util.spec_from_file_location("select_test_roots", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load select_test_roots.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

plan = _mod.plan
resolve_roots = _mod.resolve_roots
full_plan = _mod.full_plan

_REPO = Path(__file__).resolve().parents[2]


def test_full_scope_is_the_full_suite():
    matrix, _note = plan("full", "", str(_REPO))
    assert [entry["slice"] for entry in matrix] == [
        f"{i}/8" for i in range(1, 9)
    ]
    assert {entry["paths"] for entry in matrix} == {"tests"}


def test_unknown_scope_fails_closed_to_full():
    for scope in ("", "none", "garbage", "FULL"):
        matrix, _note = plan(scope, '[["tests/agent/"]]', str(_REPO))
        assert {entry["paths"] for entry in matrix} == {"tests"}, scope


def test_unparseable_or_empty_roots_fail_closed_to_full():
    for roots in ("", "not json", '{"a": 1}', '[1, 2]', '[["/nonexistent/xyz"]]'):
        matrix, note = plan("selective", roots, str(_REPO))
        assert {entry["paths"] for entry in matrix} == {"tests"}, (roots, note)


def test_selective_resolution_includes_ci_self_tests_and_subtree():
    groups = [["tests/cron/test_jobs*.py", "tests/test_jobs*.py", "tests/cron/"]]
    matrix, note = plan("selective", json.dumps(groups), str(_REPO))
    paths = set(matrix[0]["paths"].split(":"))
    # The mirrored subtree and the always-included CI self-tests are present.
    assert "tests/cron/" in paths
    assert "tests/ci/" in paths
    # Twin globs expanded to real files.
    assert any(p.startswith("tests/cron/test_jobs") for p in paths)
    # The whole point of selective mode: strictly less than the full suite.
    all_paths = {p for entry in matrix for p in entry["paths"].split(":")}
    assert "tests" not in all_paths
    assert len(matrix) < 8


def test_missing_candidates_are_dropped_not_fatal(tmp_path):
    (tmp_path / "tests" / "ci").mkdir(parents=True)
    (tmp_path / "tests" / "ci" / "test_x.py").write_text("def test_x(): pass\n")
    roots = resolve_roots(
        [["tests/does_not_exist/test_none*.py", "tests/ci/"]], str(tmp_path)
    )
    assert roots == ["tests/ci/"]


def test_only_always_include_surviving_fails_closed(tmp_path):
    # Every group candidate missed; only tests/ci/ resolved. That is mapping
    # drift, not a valid selection — run everything.
    (tmp_path / "tests" / "ci").mkdir(parents=True)
    (tmp_path / "tests" / "ci" / "test_x.py").write_text("def test_x(): pass\n")
    matrix, note = plan(
        "selective", json.dumps([["tests/gone/", "tests/test_gone*.py"]]), str(tmp_path)
    )
    assert {entry["paths"] for entry in matrix} == {"tests"}
    assert "failing closed" in note


def test_slice_count_scales_with_resolved_files():
    # tests/hermes_cli/ is the largest mirrored subtree (~900 files): it must
    # fan out to more than one slice but never beyond the full-suite width.
    matrix, _note = plan("selective", json.dumps([["tests/hermes_cli/"]]), str(_REPO))
    assert 1 < len(matrix) <= 8
    for entry in matrix:
        i, n = entry["slice"].split("/")
        assert int(n) == len(matrix)
        assert 1 <= int(i) <= len(matrix)


def test_main_writes_github_output(tmp_path, monkeypatch, capsys):
    out = tmp_path / "ghout.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("PY_SCOPE", "full")
    monkeypatch.setenv("PY_ROOTS", "")
    monkeypatch.setenv("REPO_ROOT", str(_REPO))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert _mod.main() == 0
    written = out.read_text(encoding="utf-8")
    assert written.startswith("matrix=")
    matrix = json.loads(written.removeprefix("matrix=").strip())
    assert len(matrix) == 8
