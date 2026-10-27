"""Background-review memory proposals expire, collapse latest-wins per entry, and are shown to
the next review (#117894, #145377)."""

import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import background_review as bg
from hermes_cli.write_approval_commands import handle_pending_subcommand
from tools import write_approval as wa
from tools.memory_tool import load_on_disk_store, memory_tool
from tools.memory_tool_pending import review_pending_digest
from tools.skill_provenance import (reset_current_write_origin, reset_review_attended,
                                    set_current_write_origin, set_review_attended)

_A = "Staging DB: pg-staging-2 (old cluster, retiring)"
_B = "Deploy host: build-01 (decommissioned)"
_C = "Repo lives in ~/src/app; tests via make test"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    store = load_on_disk_store()
    for entry in (_A, _B, _C):
        assert store.add("memory", entry)["success"]
    return store


def _review(store, **kwargs):
    origin, attended = set_current_write_origin("background_review"), set_review_attended(False)
    try:
        result = json.loads(memory_tool(target="memory", store=store, **kwargs))
    finally:
        reset_review_attended(attended)
        reset_current_write_origin(origin)
    assert result.get("staged") is True, result
    return result


def _backdate(pending_id, days):
    path = wa._pending_path(wa.MEMORY, pending_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["created_at"] = time.time() - days * 86400
    path.write_text(json.dumps(record), encoding="utf-8")


def test_old_review_proposals_expire_but_foreground_writes_stay(store, caplog):
    old = _review(store, action="remove", old_text="Staging DB")["pending_id"]
    fresh = _review(store, action="remove", old_text="Deploy host")["pending_id"]
    foreground = wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": "x"},
                                summary="add x", origin="foreground")["id"]
    for pid in (old, foreground):
        _backdate(pid, 30)

    with caplog.at_level(logging.INFO, logger="tools.memory_tool_pending"):
        listing = handle_pending_subcommand(wa.MEMORY, ["pending"])

    assert old not in listing and fresh in listing and foreground in listing
    assert wa.get_pending(wa.MEMORY, old) is None
    assert any(old in rec.getMessage() and "Expired" in rec.getMessage() for rec in caplog.records)


def test_ttl_zero_keeps_proposals(store):
    from hermes_cli.config import load_config, save_config
    config = load_config()
    config.setdefault("memory", {})["pending_ttl_days"] = 0
    save_config(config)
    pid = _review(store, action="remove", old_text="Staging DB")["pending_id"]
    _backdate(pid, 365)
    assert pid in handle_pending_subcommand(wa.MEMORY, ["pending"])


def test_new_proposal_for_the_same_entry_replaces_the_older_one(store):
    first = _review(store, action="patch", old_text="pg-staging-2", content="pg-staging-3")["pending_id"]
    second = _review(store, operations=[{"action": "remove", "old_text": "Staging DB"},
                                        {"action": "add", "content": "Staging DB: pg-staging-4"}])

    assert second["superseded"] == [first]
    assert [r["id"] for r in wa.list_pending(wa.MEMORY)] == [second["pending_id"]]


def test_older_proposal_that_also_targets_other_entries_is_kept(store):
    both = _review(store, operations=[{"action": "remove", "old_text": "Staging DB"},
                                      {"action": "remove", "old_text": "Deploy host"}])["pending_id"]
    one = _review(store, action="remove", old_text="Staging DB")

    assert "superseded" not in one
    assert {r["id"] for r in wa.list_pending(wa.MEMORY)} == {both, one["pending_id"]}


def test_review_prompt_lists_its_own_pending_proposals(store):
    assert review_pending_digest() == ""
    pid = _review(store, action="remove", old_text="Staging DB")["pending_id"]
    wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": "user's own"},
                   summary="add", origin="foreground")
    captured = {}

    def fake_build(agent, task_cfg=None, *, max_iterations, write_origin="background_review"):
        fork = SimpleNamespace(_memory_enabled=True, _user_profile_enabled=False, _session_messages=[],
                               run_conversation=lambda **kw: captured.update(kw))
        return fork, {}, False

    noop = lambda *a, **k: None
    with patch.object(bg, "build_cache_parity_fork", fake_build), \
            patch.object(bg, "_track_review_fork", noop), \
            patch.object(bg, "_snapshot_review_usage", lambda a: {}), \
            patch.object(bg, "_record_review_usage_to_parent", noop), \
            patch.object(bg, "finish_background_review_run", noop), \
            patch.object(bg, "_release_fork_clients", noop):
        bg._run_review_fork(SimpleNamespace(), [], "REVIEW", None, None, bg._ReviewForkState(), True)

    message = captured["user_message"]
    assert message.startswith("REVIEW") and "do not stage them again" in message
    assert pid in message and "remove «Staging DB: pg-staging-2" in message
    assert "user's own" not in message
