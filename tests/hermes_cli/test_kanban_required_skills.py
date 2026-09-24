"""Mandatory per-task skills (``required_skills``) — authoring, spawn and
typed dispatcher handling.

Contract under test (Slice 4 of the high-ROI process plan):

* ``required_skills`` is a distinct, opt-in task field: empty by default,
  advisory ``skills`` unchanged.
* The spawn argv carries one ``--required-skills <name>`` pair per declared
  name (deduplicated against the advisory ``--skills`` pairs).
* A worker exiting ``KANBAN_MISSING_SKILL_EXIT_CODE`` (BSD EX_CONFIG; the
  worker's startup gate raises when a required skill is missing/disabled)
  is a TYPED dispatcher outcome: the task is blocked as ``capability``,
  never counted as a failure, never tripping the circuit breaker, and the
  run outcome is ``missing_skill`` — not ``crashed``.
* Review admission: a reviewerless ``request_review`` is refused while
  automatic review dispatch is enabled (see test_kanban_review_lifecycle.py
  for the transition-side tests); legacy ``review`` rows without reviewer
  provenance are skipped by the review dispatch lane instead of being
  auto-spawned for the implementer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


# ---------------------------------------------------------------------------
# Schema / authoring
# ---------------------------------------------------------------------------


def test_create_task_persists_required_skills(kanban_home):
    """required_skills round-trips through create/get and defaults to None."""
    with kbc.connect() as conn:
        plain = kb.create_task(conn, title="plain", assignee="a")
        assert kb.get_task(conn, plain).required_skills is None

        tid = kb.create_task(
            conn, title="with required", assignee="a",
            skills=["advisory"], required_skills=["alpha", "beta"],
        )
        task = kb.get_task(conn, tid)
        assert task.skills == ["advisory"]
        assert task.required_skills == ["alpha", "beta"]
        # Provenance rides the created event.
        created = [e for e in kb.list_events(conn, tid) if e.kind == "created"][-1]
        assert created.payload["required_skills"] == ["alpha", "beta"]
        assert created.payload["skills"] == ["advisory"]


def test_legacy_db_without_required_skills_column_migrates(tmp_path):
    """_migrate_add_optional_columns adds required_skills on legacy DBs."""
    import sqlite3
    import hermes_cli.kanban_db_connect as _kbc

    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT, "
        "created_at INTEGER NOT NULL)"
    )
    conn.commit()
    before = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "required_skills" not in before
    _kbc._migrate_add_optional_columns(conn)
    after = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "required_skills" in after
    conn.close()


def test_authoring_validation_flags_missing_required_skills(kanban_home):
    """Advisory authoring check: names with no SKILL.md under the assignee
    profile's (or the bundled) skills tree are reported; existing bundled
    skills resolve. Plugin-qualified and path-like names are skipped."""
    missing = kb.validate_required_skills_for_assignee(
        "some-assignee", ["definitely-not-installed-xyz", "also-missing-abc"],
    )
    assert missing == ["definitely-not-installed-xyz", "also-missing-abc"]
    # Empty / None declares validate trivially.
    assert kb.validate_required_skills_for_assignee("a", []) == []
    assert kb.validate_required_skills_for_assignee("a", None) == []
    # Plugin-qualified / path-like names are the startup gate's job.
    assert kb.validate_required_skills_for_assignee("a", ["plug:skill", "a/b"]) == []


# ---------------------------------------------------------------------------
# Spawn argv
# ---------------------------------------------------------------------------


def test_spawn_argv_carries_required_skills(kanban_home, monkeypatch):
    """One --required-skills pair per declared name, deduplicated against
    the advisory --skills pairs; absent when none declared."""
    captured = {}

    class FakeProc:
        pid = 99999

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    from hermes_cli import kanban_db_workspace as kbw

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="req", assignee="p",
            skills=["shared", "only-advisory"], required_skills=["shared", "must-have"],
        )
        task = kb.get_task(conn, tid)
        workspace = kbw.resolve_workspace(task)
        kbd._default_spawn(task, str(workspace))

    cmd = captured["cmd"]
    # Advisory pairs unchanged.
    assert cmd.count("--skills") == 2
    # Required pair only for names not already carried advisory.
    req_positions = [i for i, tok in enumerate(cmd) if tok == "--required-skills"]
    assert sorted(cmd[i + 1] for i in req_positions) == ["must-have", "shared"]


def test_spawn_argv_no_required_flag_when_none_declared(kanban_home, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 99999

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    from hermes_cli import kanban_db_workspace as kbw

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="p")
        task = kb.get_task(conn, tid)
        workspace = kbw.resolve_workspace(task)
        kbd._default_spawn(task, str(workspace))

    assert "--required-skills" not in captured["cmd"]


# ---------------------------------------------------------------------------
# Typed dispatcher handling
# ---------------------------------------------------------------------------


def test_missing_skill_exit_blocks_capability_without_breaker(kanban_home, monkeypatch):
    """A worker exiting KANBAN_MISSING_SKILL_EXIT_CODE books as a typed
    ``missing_skill`` outcome: blocked with kind=capability, run outcome
    missing_skill (never crashed), no failure counted — repeatedly."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kbc.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(
            conn, title="ms", assignee="a", required_skills=["not-there"],
        )
        for i in range(3):
            pid = 71000 + i
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=0 WHERE id=?",
                (pid, tid),
            )
            conn.commit()
            kbd._record_worker_exit(
                pid, _exited_status(_kb.KANBAN_MISSING_SKILL_EXIT_CODE)
            )
            crashed = kbd.detect_crashed_workers(conn)
            assert tid not in crashed

            task = kb.get_task(conn, tid)
            assert task.status == "blocked", f"hit {i}: {task.status}"
            assert task.block_kind == "capability"
            assert task.consecutive_failures == 0, (
                f"hit {i}: missing-skill must not count a failure"
            )

        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "missing_skill" in outcomes
        assert "crashed" not in outcomes
        # A blocked event named the capability cause.
        blocked_events = [
            e for e in kb.list_events(conn, tid) if e.kind == "blocked"
        ]
        assert blocked_events, "expected a blocked event"
        assert all(e.payload.get("kind") == "capability" for e in blocked_events)


def test_classify_worker_exit_missing_skill(kanban_home):
    """The classifier maps the typed exit code to the missing_skill kind."""
    import hermes_cli.kanban_db as _kb

    pid = 72001
    kbd._record_worker_exit(pid, _exited_status(_kb.KANBAN_MISSING_SKILL_EXIT_CODE))
    kind, code = kbd._classify_worker_exit(pid)
    assert (kind, code) == ("missing_skill", _kb.KANBAN_MISSING_SKILL_EXIT_CODE)


# ---------------------------------------------------------------------------
# Review dispatch lane guard (legacy rows)
# ---------------------------------------------------------------------------


def test_review_lane_skips_rows_without_reviewer_provenance(kanban_home, monkeypatch):
    """A review row with no review_requested event naming a reviewer is
    skipped (parked for explicit routing), not auto-spawned for its current
    assignee; a row WITH provenance dispatches normally."""
    spawned = []

    def fake_dispatch_lane_task(conn, row, assignee, result, *, lane, **kwargs):
        spawned.append((row["id"], lane))
        return True

    monkeypatch.setattr(kbd, "_dispatch_lane_task", fake_dispatch_lane_task)

    with kbc.connect() as conn:
        legacy = kb.create_task(conn, title="legacy review", assignee="impl")
        conn.execute(
            "UPDATE tasks SET status='review' WHERE id=?", (legacy,)
        )
        conn.commit()
        routed = kb.create_task(conn, title="routed review", assignee="impl")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (routed,))
        kb._append_event(conn, routed, "review_requested", {"reviewer": "rev1"})
        conn.commit()

        result = kbd.dispatch_once(conn)
        assert legacy in result.skipped_review_missing_reviewer
        assert legacy not in spawned
        assert (routed, "review") in spawned
