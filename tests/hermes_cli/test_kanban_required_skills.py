"""Mandatory worker skills block before work and do not consume the crash budget."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def test_required_skills_persist_and_spawn(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="mandatory", assignee="worker",
                             skills=["advisory"], required_skills=["needed"])
        task = kb.get_task(conn, tid)
        assert task.skills == ["advisory"]
        assert task.required_skills == ["needed"]
        argv = kbd._worker_argv(task, "worker", None)
        assert argv[argv.index("--required-skills") + 1] == "needed"
        assert argv[argv.index("--skills") + 1] == "advisory"
        assert kb.get_task(conn, kb.create_task(conn, title="plain", assignee="worker")).required_skills is None


def test_missing_required_skill_exit_blocks_without_crash_budget(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="mandatory", assignee="worker", required_skills=["needed"])
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:mock") is not None
        pid = 77001
        kbd._set_worker_pid(conn, tid, pid)
        kbd._record_worker_exit(pid, kb.KANBAN_MISSING_SKILL_EXIT_CODE << 8)
        assert kbd.detect_crashed_workers(conn) == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        assert task.consecutive_failures == 0
        assert tid in kbd.detect_crashed_workers._last_missing_skill
        assert conn.execute("SELECT outcome FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == "missing_skill"


@pytest.mark.parametrize("quiet", [False, True])
def test_single_query_missing_skill_exits_typed_before_claim(monkeypatch, quiet):
    import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_should_seed_interactive", lambda *args: False)
    cli = MagicMock()
    cli._required_skills_requested = ["needed"]
    cli.finalize_preloaded_skills.side_effect = cli_mod.RequiredSkillError("needed")
    cli._claim_active_session.side_effect = AssertionError("agent work started")
    with pytest.raises(SystemExit) as exc:
        cli_mod._run_single_query_mode(cli, "task", None, quiet, True)
    assert exc.value.code == kb.KANBAN_MISSING_SKILL_EXIT_CODE
    cli._claim_active_session.assert_not_called()
    cli.chat.assert_not_called()
    cli._init_agent.assert_not_called()


def test_required_missing_among_loaded_refuses_advisory_partial_continues():
    import cli as cli_mod

    def preload(required):
        cli = type("Preload", (), {})()
        cli.system_prompt = "base"
        cli._preload_skills_thread = MagicMock()
        cli._preload_skills_thread.is_alive.return_value = False
        cli._preload_skills_result = ("skill prompt", ["present"], ["missing"])
        cli._required_skills_requested = required
        cli_mod.HermesCLI.finalize_preloaded_skills(cli)
        return cli

    assert preload([]).preloaded_skills == ["present"]
    with pytest.raises(cli_mod.RequiredSkillError, match="missing"):
        preload(["missing"])
