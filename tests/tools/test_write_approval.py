"""Tests for the memory/skill write-approval gate (tools/write_approval.py)
and the shared slash-command handlers (hermes_cli/write_approval_commands.py).

Covers the boolean write_approval gate (off by default = write freely; on =
require approval) for both subsystems, the foreground-vs-background staging
split, pending store CRUD, and the list/approve/reject/diff/approval
subcommand dispatch.
"""

import json
import os
import tempfile
import shutil
import threading
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_wa_test_")
    home = os.path.join(d, ".hermes")
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(d, ignore_errors=True)


def _set_approval(subsystem, enabled):
    import hermes_cli.config as cfg
    c = cfg.load_config()
    c.setdefault(subsystem, {})["write_approval"] = enabled
    cfg.save_config(c)


def _apply_staged_skill_record(wa, smt, pending_id):
    def apply_current(record):
        result = json.loads(smt.apply_skill_pending(record["payload"]))
        return bool(result.get("success")), result.get("error", "")

    return wa.apply_pending_record(wa.SKILLS, pending_id, apply_current)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def test_default_gate_is_off(hermes_home):
    from tools import write_approval as wa
    # Default: gate off → writes flow freely.
    assert wa.write_approval_enabled("memory") is False
    assert wa.write_approval_enabled("skills") is False


def test_invalid_subsystem_fails_closed(hermes_home):
    from tools import write_approval as wa
    assert wa.write_approval_enabled("bogus") is True


def test_normalize_enabled_coerces_values():
    from tools import write_approval as wa
    # Real bools pass through.
    assert wa._normalize_enabled(True) is True
    assert wa._normalize_enabled(False) is False
    # Truthy strings → True (incl. legacy 'approve').
    assert wa._normalize_enabled("on") is True
    assert wa._normalize_enabled("approve") is True
    assert wa._normalize_enabled("true") is True
    # Explicit false values disable the gate; malformed values fail closed.
    assert wa._normalize_enabled("off") is False
    assert wa._normalize_enabled("garbage") is True
    assert wa._normalize_enabled(None) is True


# ---------------------------------------------------------------------------
# Memory gate
# ---------------------------------------------------------------------------

def test_memory_gate_off_allows_write(hermes_home):
    # Default (gate off) → write straight through, no staging.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "user", "save me", store=store))
    assert r["success"] is True
    assert r["entry_count"] == 1
    assert wa.pending_count("memory") == 0


def test_cli_memory_approve_without_live_agent_uses_fresh_store(hermes_home, capsys):
    """#46783: ``/memory approve`` from a context with no live agent (e.g. the
    Desktop GUI) passed ``memory_store=None`` into the shared handler, which
    returned "memory store unavailable" and applied nothing. The CLI handler must
    fall back to a freshly loaded on-disk store, like the gateway path does."""
    import json
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    _set_approval("memory", True)
    staging = MemoryStore(); staging.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "remember the launch date", store=staging))
    assert r.get("pending_id"), r
    assert wa.pending_count("memory") == 1

    # Bare CLI handler with no live agent → store resolves to None pre-fix.
    handler = CLICommandsMixin.__new__(CLICommandsMixin)
    handler.agent = None
    handler._handle_memory_command("/memory approve all")

    out = capsys.readouterr().out
    assert "memory store unavailable" not in out, out
    assert "Approved 1" in out, out
    assert wa.pending_count("memory") == 0
    # The approved write landed in a freshly loaded on-disk store (MEMORY.md).
    reloaded = MemoryStore(); reloaded.load_from_disk()
    assert any("remember the launch date" in e for e in reloaded.memory_entries)


def test_load_on_disk_store_honors_configured_limits_and_permissions(hermes_home, monkeypatch):
    """Fresh approval stores must match the live agent's limits and target gates."""
    from tools.memory_tool import load_on_disk_store

    # Config override path: helper picks up configured limits and store flags.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "memory": {
                "memory_char_limit": 999,
                "user_char_limit": 444,
                "memory_enabled": False,
                "user_profile_enabled": True,
            }
        },
    )
    store = load_on_disk_store()
    assert store.memory_char_limit == 999
    assert store.user_char_limit == 444
    assert store.memory_enabled is False
    assert store.user_profile_enabled is True

    # Failure path: config raises → defaults, never blows up.
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    fallback = load_on_disk_store()
    assert fallback.memory_char_limit == 2200
    assert fallback.user_char_limit == 1375
    assert fallback.memory_enabled is True
    assert fallback.user_profile_enabled is True


# ---------------------------------------------------------------------------
# Skill gate
# ---------------------------------------------------------------------------

_SKILL = (
    "---\nname: test-skill\ndescription: A test skill\nversion: 1.0.0\n---\n"
    "# Test\nbody\n"
)


# ---------------------------------------------------------------------------
# Pending store CRUD
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared command handler
# ---------------------------------------------------------------------------


def test_handle_approve_all(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    from tools.memory_tool import _build_memory_write_guard

    wa.stage_write(
        "memory",
        {
            "action": "add",
            "target": "user",
            "content": "a",
            "_write_guard": _build_memory_write_guard(store, "user"),
        },
        summary="a",
        origin="foreground",
    )
    wa.stage_write(
        "memory",
        {
            "action": "add",
            "target": "memory",
            "content": "b",
            "_write_guard": _build_memory_write_guard(store, "memory"),
        },
        summary="b",
        origin="foreground",
    )
    out = handle_pending_subcommand(wa.MEMORY, ["approve", "all"], memory_store=store)
    assert "Approved 2" in out
    assert wa.pending_count("memory") == 0
    assert store.user_entries == ["a"]
    assert store.memory_entries == ["b"]


def test_handle_approval_on(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.MEMORY, ["approval", "on"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is True
    assert "on" in out


def test_handle_approval_off(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.SKILLS, ["approval", "off"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is False
    assert "off" in out


# ---------------------------------------------------------------------------
# Inline (interactive CLI) approval path — regression for the bug where the
# per-thread approval callback was never passed to prompt_dangerous_approval,
# so every gated foreground memory write was silently denied.
# ---------------------------------------------------------------------------

@pytest.fixture
def approval_callback_cleanup():
    yield
    from tools.terminal_tool import set_approval_callback
    set_approval_callback(None)


def test_memory_inline_approve_writes(hermes_home, approval_callback_cleanup):
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)

    calls = []
    def approve_cb(command, description, **kw):
        calls.append((command, description))
        return "once"
    set_approval_callback(approve_cb)

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "approved fact", store=store))
    assert r["success"] is True
    assert r.get("staged") is None  # real write, not staged
    assert store.memory_entries == ["approved fact"]
    assert wa.pending_count("memory") == 0
    # The registered callback must actually be invoked (not the input() path).
    assert len(calls) == 1
    assert "approved fact" in calls[0][0]


def test_memory_inline_deny_blocks(hermes_home, approval_callback_cleanup):
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)
    set_approval_callback(lambda command, description, **kw: "deny")

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "denied fact", store=store))
    assert r["success"] is False
    assert "denied" in r["error"].lower()
    assert store.memory_entries == []
    assert wa.pending_count("memory") == 0  # denied, not staged


def test_memory_invalid_params_rejected_before_staging(hermes_home):
    # Param validation must run BEFORE the gate so a broken write is rejected
    # immediately instead of staged and failing at approve time.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    _set_approval("memory", True)
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", None, store=store))
    assert r["success"] is False
    assert wa.pending_count("memory") == 0


class TestSkillGist:
    """skill_gist builds a heuristic one-line summary for a pending skill write.

    Pure, no model call — every branch is verifiable from the function source.
    """

    def test_create_with_frontmatter_description(self):
        from tools import write_approval as wa
        content = "---\ndescription: My cool skill\n---\nprint('hi')\n"
        assert (
            wa.skill_gist("create", "demo", content=content)
            == f"create 'demo' — My cool skill ({len(content)} chars)"
        )

    def test_edit_without_description_uses_size_only(self):
        from tools import write_approval as wa
        content = "no frontmatter here"
        assert (
            wa.skill_gist("edit", "demo", content=content)
            == f"rewrite 'demo' ({len(content)} chars)"
        )


    def test_file_actions_and_unknown_fallback(self):
        from tools import write_approval as wa
        assert wa.skill_gist("write_file", "demo", file_path="a.py") == "write a.py in 'demo'"
        assert wa.skill_gist("remove_file", "demo", file_path="a.py") == "remove a.py from 'demo'"
        assert wa.skill_gist("delete", "demo") == "delete skill 'demo'"
        assert wa.skill_gist("unknown", "demo") == "unknown 'demo'"


def test_config_read_failure_fails_closed(hermes_home, monkeypatch):
    from tools import write_approval as wa

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert wa.write_approval_enabled("skills") is True


def test_pending_write_is_private_verified_and_deduplicated(hermes_home):
    from tools import write_approval as wa

    payload = {"action": "patch", "name": "demo", "old_string": "a", "new_string": "b"}
    first = wa.stage_write(wa.SKILLS, payload, summary="demo patch", origin="background_review")
    second = wa.stage_write(wa.SKILLS, payload, summary="same patch", origin="foreground")
    path = os.path.join(hermes_home, "pending", "skills", f"{first['id']}.json")
    assert first["persisted"] is True
    assert second["id"] == first["id"]
    assert second["deduplicated"] is True
    assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_pending_dedupe_binds_stage_context_per_run(hermes_home):
    from tools import write_approval as wa

    base = {"action": "patch", "name": "demo", "old_string": "a", "new_string": "b"}
    run_one = {**base, "_stage_context": {"memory_hygiene_run_id": "run-one"}}
    same_run = dict(run_one)
    run_two = {**base, "_stage_context": {"memory_hygiene_run_id": "run-two"}}
    first = wa.stage_write(wa.SKILLS, run_one, summary="one", origin="background_review")
    duplicate = wa.stage_write(
        wa.SKILLS, same_run, summary="same", origin="background_review"
    )
    distinct = wa.stage_write(wa.SKILLS, run_two, summary="two", origin="background_review")
    assert duplicate["id"] == first["id"]
    assert duplicate["deduplicated"] is True
    assert distinct["id"] != first["id"]


def test_corrupt_pending_record_is_not_trusted_for_dedupe(hermes_home):
    from tools import write_approval as wa

    payload = {"action": "patch", "name": "demo", "old_string": "a", "new_string": "b"}
    first = wa.stage_write(wa.SKILLS, payload, summary="one", origin="background_review")
    path = os.path.join(hermes_home, "pending", "skills", f"{first['id']}.json")
    record = json.loads(open(path, encoding="utf-8").read())
    record["payload"]["new_string"] = "tampered"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    second = wa.stage_write(wa.SKILLS, payload, summary="two", origin="background_review")
    assert second["id"] != first["id"]
    assert second["deduplicated"] is False


def test_pending_disk_failure_never_reports_staged(hermes_home, monkeypatch):
    from tools import write_approval as wa

    monkeypatch.setattr(wa.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(wa.PendingWriteError, match="durably stage"):
        wa.stage_write(
            wa.SKILLS,
            {"action": "patch", "name": "demo"},
            summary="must fail",
            origin="background_review",
        )


def test_staged_skill_apply_rejects_stale_target(hermes_home):
    from tools import skill_manager_tool as smt
    from tools import write_approval as wa

    skill_dir = os.path.join(hermes_home, "skills", "demo")
    os.makedirs(skill_dir)
    skill_md = os.path.join(skill_dir, "SKILL.md")
    original = "---\nname: demo\ndescription: Use when testing staging.\nversion: 1.0.0\n---\n# Demo\nold\n"
    with open(skill_md, "w", encoding="utf-8") as handle:
        handle.write(original)
    record = smt.stage_skill_write(
        {"action": "patch", "name": "demo", "old_string": "old", "new_string": "new"},
        summary="patch demo",
        origin="foreground",
    )
    with open(skill_md, "w", encoding="utf-8") as handle:
        handle.write(original.replace("old", "foreign"))
    ok, message = _apply_staged_skill_record(wa, smt, record["id"])
    assert ok is False
    assert "stale" in message.lower()
    assert wa.get_pending(wa.SKILLS, record["id"]) is not None
    assert "foreign" in open(skill_md, encoding="utf-8").read()


def test_staged_skill_apply_uses_target_cas_and_succeeds_once(hermes_home):
    from tools import skill_manager_tool as smt
    from tools import write_approval as wa

    skill_dir = os.path.join(hermes_home, "skills", "demo")
    os.makedirs(skill_dir)
    skill_md = os.path.join(skill_dir, "SKILL.md")
    original = "---\nname: demo\ndescription: Use when testing staging.\nversion: 1.0.0\n---\n# Demo\nold\n"
    with open(skill_md, "w", encoding="utf-8") as handle:
        handle.write(original)
    record = smt.stage_skill_write(
        {"action": "patch", "name": "demo", "old_string": "old", "new_string": "new"},
        summary="patch demo",
        origin="foreground",
    )
    first_ok, first_message = _apply_staged_skill_record(wa, smt, record["id"])
    second_ok, second_message = _apply_staged_skill_record(wa, smt, record["id"])
    assert first_ok is True, first_message
    assert wa.get_pending(wa.SKILLS, record["id"]) is None
    assert second_ok is False
    assert "disappeared" in second_message.lower()
    assert "new" in open(skill_md, encoding="utf-8").read()


def test_direct_skill_pending_apply_without_live_record_is_rejected(hermes_home):
    from tools import skill_manager_tool as smt

    result = json.loads(smt.apply_skill_pending({"action": "patch", "name": "demo"}))
    assert result["success"] is False
    assert "restage" in result["error"].lower()


def test_deleted_pending_record_cannot_mutate_skill(hermes_home):
    from tools import skill_manager_tool as smt
    from tools import write_approval as wa

    skill_dir = os.path.join(hermes_home, "skills", "demo")
    os.makedirs(skill_dir)
    skill_md = os.path.join(skill_dir, "SKILL.md")
    original = "---\nname: demo\ndescription: Use when testing.\nversion: 1.0.0\n---\nold\n"
    with open(skill_md, "w", encoding="utf-8") as handle:
        handle.write(original)
    record = smt.stage_skill_write(
        {"action": "patch", "name": "demo", "old_string": "old", "new_string": "new"},
        summary="patch demo",
        origin="foreground",
    )
    assert wa.discard_pending(wa.SKILLS, record["id"]) is True
    ok, message = _apply_staged_skill_record(wa, smt, record["id"])
    assert ok is False
    assert "disappeared" in message.lower()
    assert open(skill_md, encoding="utf-8").read() == original


def test_tampered_pending_payload_cannot_mutate_skill(hermes_home):
    from tools import skill_manager_tool as smt
    from tools import write_approval as wa

    skill_dir = os.path.join(hermes_home, "skills", "demo")
    os.makedirs(skill_dir)
    skill_md = os.path.join(skill_dir, "SKILL.md")
    original = "---\nname: demo\ndescription: Use when testing.\nversion: 1.0.0\n---\nold\n"
    with open(skill_md, "w", encoding="utf-8") as handle:
        handle.write(original)
    record = smt.stage_skill_write(
        {"action": "patch", "name": "demo", "old_string": "old", "new_string": "new"},
        summary="patch demo",
        origin="foreground",
    )
    path = os.path.join(hermes_home, "pending", "skills", f"{record['id']}.json")
    raw = json.loads(open(path, encoding="utf-8").read())
    raw["payload"]["new_string"] = "tampered"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)
    ok, message = _apply_staged_skill_record(wa, smt, record["id"])
    assert ok is False
    assert "hash mismatch" in message.lower()
    assert open(skill_md, encoding="utf-8").read() == original


def test_apply_claim_is_durable_and_failed_callback_restores_pending(
    hermes_home,
):
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "claim"},
        summary="claim",
        origin="foreground",
    )
    path = Path(hermes_home) / "pending" / "memory" / f"{record['id']}.json"
    observed = {}

    def refuse(current):
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        observed["callback_state"] = current["state"]
        observed["disk_state"] = on_disk["state"]
        return False, "synthetic refusal"

    ok, message = wa.apply_pending_record(wa.MEMORY, record["id"], refuse)
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert ok is False
    assert message == "synthetic refusal"
    assert observed == {"callback_state": "applying", "disk_state": "applying"}
    assert restored["state"] == "pending"
    assert "applying_started_at" not in restored


def test_callback_exception_restores_pending_record(hermes_home):
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "claim"},
        summary="claim",
        origin="foreground",
    )

    def explode(_current):
        raise RuntimeError("synthetic callback failure")

    ok, message = wa.apply_pending_record(wa.MEMORY, record["id"], explode)
    restored = wa.get_pending(wa.MEMORY, record["id"])
    assert ok is False
    assert "synthetic callback failure" in message
    assert restored is not None
    assert restored.get("state", "pending") == "pending"


def test_consume_failure_quarantines_and_blocks_replay_and_reject(
    hermes_home, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "claim"},
        summary="claim",
        origin="foreground",
    )
    path = Path(hermes_home) / "pending" / "memory" / f"{record['id']}.json"
    real_unlink = Path.unlink

    def fail_record_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError("synthetic consume failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_record_unlink)
    calls = {"count": 0}

    def succeeds(current):
        calls["count"] += 1
        assert wa.consume_pending_apply_capability(wa.MEMORY, current["payload"])
        return True, "target applied"

    first_ok, first_message = wa.apply_pending_record(
        wa.MEMORY, record["id"], succeeds
    )
    second_ok, second_message = wa.apply_pending_record(
        wa.MEMORY, record["id"], succeeds
    )
    quarantined = wa.get_pending(wa.MEMORY, record["id"])
    pending_output = handle_pending_subcommand(wa.MEMORY, ["pending"])
    reject_output = handle_pending_subcommand(
        wa.MEMORY, ["reject", record["id"]]
    )

    assert first_ok is False
    assert "may already be mutated" in first_message
    assert second_ok is False
    assert "quarantined" in second_message
    assert calls["count"] == 1
    assert quarantined is not None and quarantined["state"] == "applying"
    assert pending_output is not None and "recovery required" in pending_output
    assert reject_output is not None and "reconcile its target" in reject_output
    assert wa.discard_pending(wa.MEMORY, record["id"]) is False


def test_success_without_capability_consumption_is_not_committed(hermes_home):
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "claim"},
        summary="claim",
        origin="foreground",
    )

    ok, message = wa.apply_pending_record(
        wa.MEMORY,
        record["id"],
        lambda _current: (True, "dishonest success"),
    )
    restored = wa.get_pending(wa.MEMORY, record["id"])
    assert ok is False
    assert "without consuming" in message
    assert restored is not None and restored.get("state", "pending") == "pending"


def test_failure_after_capability_consumption_is_quarantined(hermes_home):
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "claim"},
        summary="claim",
        origin="foreground",
    )

    def ambiguous_failure(current):
        assert wa.consume_pending_apply_capability(wa.MEMORY, current["payload"])
        return False, "synthetic late failure"

    ok, message = wa.apply_pending_record(
        wa.MEMORY, record["id"], ambiguous_failure
    )
    quarantined = wa.get_pending(wa.MEMORY, record["id"])
    assert ok is False
    assert "remains quarantined" in message
    assert quarantined is not None and quarantined["state"] == "applying"


def test_pending_apply_capability_cannot_be_forged_outside_record_apply(hermes_home):
    from tools import write_approval as wa

    payload = {"action": "add", "target": "memory", "content": "claim"}
    assert wa.consume_pending_apply_capability(wa.MEMORY, payload) is False


def test_direct_memory_pending_apply_without_live_record_is_rejected(hermes_home):
    from tools.memory_tool import apply_memory_pending

    class EnabledStore:
        def target_enabled(self, _target):
            return True

        def add(self, *_args):
            raise AssertionError("direct replay reached the memory store")

    result = apply_memory_pending(
        {"action": "add", "target": "memory", "content": "claim"},
        EnabledStore(),  # type: ignore[arg-type]
    )
    assert result["success"] is False
    assert result["target"] == "memory"
    assert "restage" in result["error"]


def test_dedupe_into_applying_record_surfaces_recovery_required(hermes_home):
    from tools import write_approval as wa

    payload = {"action": "add", "target": "memory", "content": "claim"}
    record = wa.stage_write(
        wa.MEMORY, payload, summary="claim", origin="foreground"
    )
    observed = {}

    def inspect_while_claimed(_current):
        duplicate = wa.stage_write(
            wa.MEMORY, payload, summary="retry", origin="foreground"
        )
        observed.update(duplicate)
        return False, "leave retryable"

    ok, _ = wa.apply_pending_record(
        wa.MEMORY, record["id"], inspect_while_claimed
    )
    assert ok is False
    assert observed["id"] == record["id"]
    assert observed["deduplicated"] is True
    assert observed["recovery_required"] is True


def test_pending_id_collision_never_overwrites_existing_record(
    hermes_home, monkeypatch
):
    from tools import write_approval as wa

    values = iter(["a" * 32, "b" * 32, "a" * 32, "c" * 32, "d" * 32])

    class FakeUUID:
        def __init__(self, value):
            self.hex = value

    monkeypatch.setattr(wa.uuid, "uuid4", lambda: FakeUUID(next(values)))
    first = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "one"},
        summary="one",
        origin="foreground",
    )
    second = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "two"},
        summary="two",
        origin="foreground",
    )
    assert first["id"] == "a" * 32
    assert second["id"] == "c" * 32
    first_live = wa.get_pending(wa.MEMORY, first["id"])
    second_live = wa.get_pending(wa.MEMORY, second["id"])
    assert first_live is not None
    assert second_live is not None
    assert first_live["payload"]["content"] == "one"
    assert second_live["payload"]["content"] == "two"


def test_legacy_pending_is_visible_but_not_approvable(
    hermes_home,
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    pending_id = "deadbeef"
    directory = Path(hermes_home) / "pending" / "memory"
    directory.mkdir(parents=True)
    path = directory / f"{pending_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": pending_id,
                "subsystem": "memory",
                "action": "add",
                "summary": "legacy",
                "created_at": 1,
                "payload": {
                    "action": "add",
                    "target": "memory",
                    "content": "legacy claim",
                },
            }
        ),
        encoding="utf-8",
    )
    listed = handle_pending_subcommand(wa.MEMORY, ["pending"])
    approved = handle_pending_subcommand(
        wa.MEMORY,
        ["approve", pending_id],
        memory_store=MemoryStore(),
    )
    assert listed is not None and "legacy — restage required" in listed
    assert approved is not None
    assert "Approved 0" in approved
    assert "current schema" in approved
    assert path.exists()


def test_manual_quarantine_resolution_preserves_evidence(hermes_home):
    from tools import write_approval as wa

    first = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "applied"},
        summary="applied",
        origin="foreground",
    )

    def ambiguous(current):
        assert wa.consume_pending_apply_capability(wa.MEMORY, current["payload"])
        return False, "synthetic ambiguous result"

    ok, _ = wa.apply_pending_record(wa.MEMORY, first["id"], ambiguous)
    assert ok is False
    resolved, _ = wa.resolve_applying(wa.MEMORY, first["id"], "applied")
    archive = (
        Path(hermes_home)
        / "pending"
        / "memory"
        / "resolved"
        / f"{first['id']}.json"
    )
    assert resolved is True
    assert wa.get_pending(wa.MEMORY, first["id"]) is None
    tombstone = json.loads(archive.read_text(encoding="utf-8"))
    assert tombstone["record"]["payload_sha256"] == first["payload_sha256"]

    second = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "not applied"},
        summary="not applied",
        origin="foreground",
    )
    ok, _ = wa.apply_pending_record(wa.MEMORY, second["id"], ambiguous)
    assert ok is False
    resolved, _ = wa.resolve_applying(
        wa.MEMORY, second["id"], "not-applied"
    )
    restored = wa.get_pending(wa.MEMORY, second["id"])
    assert resolved is True
    assert restored is not None and restored["state"] == "pending"


def test_memory_pending_guard_rejects_stale_target_without_consuming(
    hermes_home,
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, memory_tool

    _set_approval("memory", True)
    staging = MemoryStore()
    staging.load_from_disk()
    staged = json.loads(
        memory_tool("add", "memory", "approved fact", store=staging)
    )
    target = Path(hermes_home) / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("foreign fact", encoding="utf-8")

    current = MemoryStore()
    current.load_from_disk()
    output = handle_pending_subcommand(
        wa.MEMORY,
        ["approve", staged["pending_id"]],
        memory_store=current,
    )
    record = wa.get_pending(wa.MEMORY, staged["pending_id"])
    assert output is not None
    assert "Approved 0" in output
    assert "stale" in output.lower()
    assert target.read_text(encoding="utf-8") == "foreign fact"
    assert record is not None and record["state"] == "pending"


def test_memory_pending_guard_allows_exact_unchanged_apply(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, memory_tool

    _set_approval("memory", True)
    store = MemoryStore()
    store.load_from_disk()
    staged = json.loads(memory_tool("add", "memory", "exact fact", store=store))
    output = handle_pending_subcommand(
        wa.MEMORY,
        ["approve", staged["pending_id"]],
        memory_store=store,
    )
    assert output is not None
    assert "Approved 1" in output
    assert store.memory_entries == ["exact fact"]
    assert wa.get_pending(wa.MEMORY, staged["pending_id"]) is None


def test_skill_guard_and_mutation_share_one_writer_lock(hermes_home, monkeypatch):
    from tools import skill_manager_tool as smt
    from tools import write_approval as wa

    skill_dir = Path(hermes_home) / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    original = (
        "---\nname: demo\ndescription: Use when testing lock CAS.\n"
        "version: 1.0.0\n---\nold\n"
    )
    skill_md.write_text(original, encoding="utf-8")
    record = smt.stage_skill_write(
        {
            "action": "patch",
            "name": "demo",
            "old_string": "old",
            "new_string": "approved",
        },
        summary="approved patch",
        origin="foreground",
    )
    verified = threading.Event()
    release = threading.Event()
    real_verify = smt._verify_skill_write_guard

    def paused_verify(payload):
        real_verify(payload)
        verified.set()
        assert release.wait(5)

    monkeypatch.setattr(smt, "_verify_skill_write_guard", paused_verify)
    results = {}

    def approve():
        results["approved"] = _apply_staged_skill_record(
            wa, smt, record["id"]
        )

    def competing_write():
        results["competing"] = json.loads(
            smt.skill_manage(
                action="patch",
                name="demo",
                old_string="old",
                new_string="foreign",
            )
        )

    approval_thread = threading.Thread(target=approve)
    approval_thread.start()
    assert verified.wait(5)
    competing_thread = threading.Thread(target=competing_write)
    competing_thread.start()
    assert competing_thread.is_alive()
    release.set()
    approval_thread.join(5)
    competing_thread.join(5)
    assert not approval_thread.is_alive()
    assert not competing_thread.is_alive()
    assert results["approved"][0] is True
    assert results["competing"]["success"] is False
    assert "approved" in skill_md.read_text(encoding="utf-8")
    assert "foreign" not in skill_md.read_text(encoding="utf-8")
