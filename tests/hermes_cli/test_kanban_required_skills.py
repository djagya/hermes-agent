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
        # Since v0.22.4 the profile owner runs the card (gateway/session_kanban.py), not a
        # `hermes chat --required-skills` child: the mandatory skill rides the owner's skill list.
        from gateway.session_kanban import card_skills
        assert card_skills(task) == ["advisory", "needed"]
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


@pytest.mark.parametrize("label", ["needed", "Ambiguous skill name needed: two skills match"])
def test_owner_run_card_missing_required_skill_books_typed_exit(kanban_home, monkeypatch, label):
    """dlz D03-A: on the owner-run path a mandatory skill that did not load stops the card before
    any model turn and books exit 77 (blocked missing_skill), even when other skills loaded."""
    import json
    import os
    from types import SimpleNamespace
    from agent import skill_commands
    from gateway import session_kanban

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="mandatory", assignee="default", required_skills=["needed"])
        task = kb.claim_task(conn, tid, claimer="host:owner")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_bound", {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
    context = {"db": str(kb.kanban_db_path()), "task_id": tid, "run_id": task.current_run_id,
               "claim_lock": task.claim_lock, "skills": ["advisory", "needed"], "required_skills": ["needed"],
               "context": "", "goal_mode": False}
    # The loader reports an unresolved name by its display label (an ambiguous one is not "needed").
    monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt",
                        lambda ids, **_k: ("advisory prompt" if "advisory" in ids else "",
                                           ["advisory"] if "advisory" in ids else [], [label]))
    agent = SimpleNamespace(session_id="s1", run_conversation=MagicMock(side_effect=AssertionError("ran")))
    with pytest.raises(session_kanban.MissingRequiredSkill):
        session_kanban.run_worker_turns(agent, {"policy": {"kanban_json": json.dumps(context)}, "text": "go"}, [])
    agent.run_conversation.assert_not_called()
    assert session_kanban.worker_exit_code(kb.kanban_db_path(), context) == kb.KANBAN_MISSING_SKILL_EXIT_CODE


def test_owner_run_card_renders_each_mandatory_skill_once_into_the_prompt(monkeypatch):
    """dlz D03-A: the mandatory check reads the very load that renders the prompt. A second load
    could miss a skill that vanished meanwhile (the model then ran without it) and ran its inline
    shell snippets twice."""
    from types import SimpleNamespace
    from agent import kanban_turn_recovery, skill_commands
    from gateway import session_kanban

    rendered = []

    def load(ids, **_kwargs):
        loaded, missing = [], []
        for name in ids:
            # "needed" resolves on its first load only (deleted or disabled right after).
            (loaded if name != "needed" or "needed" not in rendered else missing).append(name)
            if name in loaded:
                rendered.append(name)
        return "\n\n".join(f"BLOCK:{n}" for n in loaded), loaded, missing

    monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt", load)
    monkeypatch.setattr(kanban_turn_recovery, "recover_failed_kanban_turns", lambda *_a, **_k: None)
    prompts = []
    agent = SimpleNamespace(session_id="s1", run_conversation=lambda prompt, **_k: prompts.append(prompt) or {
        "final_response": "ok", "completed": True})
    context = {"task_id": "t", "skills": ["advisory", "needed"], "required_skills": ["needed"],
               "context": "", "goal_mode": False}
    session_kanban._run_task_turns(agent, {"text": "go"}, [], context)
    assert rendered.count("needed") == 1
    assert "BLOCK:needed" in prompts[0] and "BLOCK:advisory" in prompts[0]


def test_owner_run_card_dedupes_mandatory_and_advisory_skills_by_file_not_name(monkeypatch):
    """dlz D03-A: ``team-a/check`` (mandatory) and ``team-b/check`` (advisory) are both ``name:
    check`` but different skills: both render. An alias of the mandatory file renders once."""
    from types import SimpleNamespace
    from agent import kanban_turn_recovery, skill_commands
    from gateway import session_kanban

    files = {"team-a/check": "/s/team-a/check", "team-b/check": "/s/team-b/check", "check": "/s/team-a/check"}

    def load(ids, excluded_loaded_names=None, **_kwargs):  # the real loader's name-keyed exclusion
        blocks = [f"BLOCK:{i}" for i in ids if "check" not in (excluded_loaded_names or ())]
        return "\n\n".join(blocks), ["check"] * len(ids), []

    monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt", load)
    monkeypatch.setattr(skill_commands, "_load_skill_payload",
                        lambda ident, **_k: ({}, files[ident], "check") if ident in files else None)
    monkeypatch.setattr(kanban_turn_recovery, "recover_failed_kanban_turns", lambda *_a, **_k: None)
    prompts = []
    agent = SimpleNamespace(session_id="s1", run_conversation=lambda prompt, **_k: prompts.append(prompt) or {
        "final_response": "ok", "completed": True})
    context = {"task_id": "t", "skills": ["team-b/check", "check", "team-a/check"],
               "required_skills": ["team-a/check"], "context": "", "goal_mode": False}
    session_kanban._run_task_turns(agent, {"text": "go"}, [], context)
    assert "BLOCK:team-b/check" in prompts[0]
    assert prompts[0].count("BLOCK:team-a/check") == 1 and "BLOCK:check" not in prompts[0]
