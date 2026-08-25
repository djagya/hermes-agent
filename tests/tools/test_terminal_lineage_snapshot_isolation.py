"""Execution-lineage isolation for shared terminal shell snapshots.

Parent agents and delegate_task children intentionally share an execution
backend. The shell snapshot may preserve user state across calls, but it must
never own delegated-child or dispatcher identity.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    KANBAN_ENV_KEYS,
    SCOPED_SUBPROCESS_ENV_MARKERS,
    delegated_child_context,
)
from tools.environments.local import LocalEnvironment


LINEAGE_ENV_KEYS = tuple(sorted({*SCOPED_SUBPROCESS_ENV_MARKERS, *KANBAN_ENV_KEYS}))


def _probe(env: LocalEnvironment) -> dict[str, str | None]:
    code = (
        "import json, os; "
        f"print(json.dumps({{key: os.environ.get(key) for key in {LINEAGE_ENV_KEYS!r}}}, "
        "sort_keys=True))"
    )
    result = env.execute(
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
        timeout=15,
    )
    assert result["returncode"] == 0, result
    return json.loads(result["output"].strip())


def _clear_lineage_env(monkeypatch) -> None:
    for name in LINEAGE_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def _assert_snapshot_has_no_lineage(env: LocalEnvironment) -> None:
    snapshot = Path(env._snapshot_path).read_text(encoding="utf-8")
    for name in LINEAGE_ENV_KEYS:
        assert name not in snapshot


def test_parent_child_parent_calls_keep_lineage_out_of_shared_snapshot(
    monkeypatch,
    tmp_path,
):
    """A valid child call must not poison the next root terminal command."""
    _clear_lineage_env(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "parent-task-sentinel")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "parent-board-sentinel")

    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        # init_session itself runs with the parent worker env, but lineage must
        # not enter the initial snapshot.
        _assert_snapshot_has_no_lineage(env)

        root_before = _probe(env)
        with delegated_child_context():
            child = _probe(env)
        root_after = _probe(env)

        assert root_before[DELEGATED_CHILD_ENV_MARKER] is None
        assert root_before["HERMES_KANBAN_TASK"] == "parent-task-sentinel"
        assert root_before["HERMES_KANBAN_BOARD"] == "parent-board-sentinel"

        assert child[DELEGATED_CHILD_ENV_MARKER] == "1"
        for name in KANBAN_ENV_KEYS:
            assert child[name] is None

        assert root_after == root_before
        _assert_snapshot_has_no_lineage(env)
    finally:
        env.cleanup()


def test_preexisting_poisoned_snapshot_is_neutralized_and_cleaned(
    monkeypatch,
    tmp_path,
):
    """The first command after upgrade must self-heal an old bad snapshot."""
    _clear_lineage_env(monkeypatch)

    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        Path(env._snapshot_path).write_text(
            '\n'.join(
                [
                    'declare -x HERMES_DELEGATED_CHILD_CONTEXT="1"',
                    'declare -x HERMES_CRON_SESSION="stale-cron-session"',
                    'declare -x HERMES_KANBAN_TASK="stale-parent-task"',
                    'declare -x HERMES_KANBAN_BOARD="stale-parent-board"',
                    '',
                ]
            ),
            encoding="utf-8",
        )

        observed = _probe(env)

        assert all(observed[name] is None for name in LINEAGE_ENV_KEYS)
        _assert_snapshot_has_no_lineage(env)
    finally:
        env.cleanup()


def test_current_lineage_wins_over_preexisting_poisoned_snapshot(
    monkeypatch,
    tmp_path,
):
    """Per-call identity must survive while stale snapshot identity is discarded."""
    _clear_lineage_env(monkeypatch)
    monkeypatch.setenv("HERMES_CRON_SESSION", "current-cron-session")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "current-parent-task")

    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        Path(env._snapshot_path).write_text(
            '\n'.join(
                [
                    'declare -x HERMES_DELEGATED_CHILD_CONTEXT="1"',
                    'declare -x HERMES_CRON_SESSION="stale-cron-session"',
                    'declare -x HERMES_KANBAN_TASK="stale-parent-task"',
                    '',
                ]
            ),
            encoding="utf-8",
        )

        observed = _probe(env)

        assert observed[DELEGATED_CHILD_ENV_MARKER] is None
        assert observed["HERMES_CRON_SESSION"] == "current-cron-session"
        assert observed["HERMES_KANBAN_TASK"] == "current-parent-task"
        _assert_snapshot_has_no_lineage(env)
    finally:
        env.cleanup()
