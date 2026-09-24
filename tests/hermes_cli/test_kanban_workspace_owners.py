"""A successor run is not admitted to a workspace a predecessor process still writes.

Ownership is re-derived from live process attributes each tick, so release needs no
lease bookkeeping and a recycled PID (no markers) never holds the workspace.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_db_workspace_owners import (
    WORKSPACE_HELD,
    WORKSPACE_OWNERSHIP_UNKNOWN,
    workspace_ownership,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN")}


@pytest.fixture
def holder(tmp_path):
    """A live non-worker process parked inside the workspace (e.g. a background job)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], cwd=workspace, env=_clean_env(),
    )
    yield SimpleNamespace(proc=proc, workspace=workspace)
    proc.kill()
    proc.wait(timeout=10)


def _task_with_ended_predecessor(conn, workspace: Path) -> str:
    task_id = kb.create_task(conn, title="t", assignee="builder")
    run = kb.claim_task(conn, task_id)
    assert kb.block_task(conn, task_id, reason="handoff", expected_run_id=run.current_run_id)
    assert kb.unblock_task(conn, task_id)
    kbw.set_workspace_path(conn, task_id, workspace)
    return task_id


@pytest.mark.linux_only
def test_live_predecessor_holds_successor_until_it_exits(kanban_home, holder, all_assignees_spawnable):
    spawned: list[str] = []

    def spawn(task, workspace):
        spawned.append(task.id)
        return 4242

    with kbc.connect_closing() as conn:
        task_id = _task_with_ended_predecessor(conn, holder.workspace)

        held = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert (task_id, WORKSPACE_HELD) in held.respawn_guarded
        assert spawned == []
        assert kb.get_task(conn, task_id).status == "ready"

        holder.proc.kill()
        holder.proc.wait(timeout=10)

        released = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert task_id in [row[0] for row in released.spawned]
    assert spawned == [task_id]


@pytest.mark.linux_only
def test_worker_env_marker_owns_even_outside_the_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = {**_clean_env(), "HERMES_KANBAN_TASK": "t_pred"}
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            cwd=tmp_path, env=env)
    try:
        ownership = workspace_ownership("t_pred", str(workspace))
        assert proc.pid in [pid for pid, _ in ownership.holders]
        # Another task's worker tree is not this task's predecessor.
        other = workspace_ownership("t_other", str(workspace))
        assert proc.pid not in [pid for pid, _ in other.holders]
    finally:
        proc.kill()
        proc.wait(timeout=10)


class _Unreadable:
    """A same-user process whose environment, cwd and files cannot be read."""

    pid = 999_999

    def status(self):
        return psutil.STATUS_SLEEPING

    def uids(self):
        return SimpleNamespace(real=os.getuid())

    def environ(self):
        raise psutil.AccessDenied(self.pid)

    cwd = open_files = environ


@pytest.mark.linux_only
def test_unreadable_ownership_fails_closed(tmp_path):
    ownership = workspace_ownership("t", str(tmp_path), processes=[_Unreadable()])
    assert ownership.guard_reason() == WORKSPACE_OWNERSHIP_UNKNOWN
    assert not ownership.released


def test_first_run_is_never_held(kanban_home, tmp_path, all_assignees_spawnable):
    """No predecessor run means no predecessor writer, whatever else is in the dir."""
    from hermes_cli.kanban_db_workspace_owners import hold_for_predecessor_writers

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="t", assignee="builder")
        kbw.set_workspace_path(conn, task_id, tmp_path)
        assert hold_for_predecessor_writers(conn, task_id) is None
