"""Unattended background-review adds stop at ``memory.background_add_ceiling``; foreground adds
do not, and the review's refusal never asks for a removal batch it cannot run (#135039)."""

import json

import pytest

from hermes_cli.config import load_config, save_config
from tools import write_approval as wa
from tools.memory_tool import load_on_disk_store, memory_tool
from tools.skill_provenance import (reset_current_write_origin, reset_review_attended,
                                    set_current_write_origin, set_review_attended)

_SEED = "Repo lives in ~/src/app; tests via make test"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    def make(ceiling=None, limit=200):
        config = load_config()
        memory = config.setdefault("memory", {})
        memory["memory_char_limit"] = limit
        if ceiling is not None:
            memory["background_add_ceiling"] = ceiling
        save_config(config)
        store = load_on_disk_store()
        assert store.add("memory", _SEED)["success"]
        return store
    return make


def _review(store, **kwargs):
    origin, attended = set_current_write_origin("background_review"), set_review_attended(False)
    try:
        return json.loads(memory_tool(target="memory", store=store, **kwargs))
    finally:
        reset_review_attended(attended)
        reset_current_write_origin(origin)


def _assert_unattended_refusal(result):
    assert result["success"] is False and result["done"] is True
    assert "skill" in result["error"] and "do not retry with a batch" in result["error"]
    assert "Retry as ONE" not in result["error"] and "current_entries" not in result


def test_review_add_past_the_ceiling_is_refused_but_foreground_add_applies(store):
    s = store(ceiling=0.5)
    fact = "Timezone: Europe/Berlin; working hours 09-18 CET"  # 95/200 chars after it; cap 100

    assert _review(s, action="add", content=fact)["success"] is True
    refused = _review(s, operations=[{"action": "add", "content": "Deploys via make ship on Fridays"}])

    _assert_unattended_refusal(refused)
    assert "background_add_ceiling = 0.5" in refused["error"]
    assert s.memory_entries == [_SEED, fact]
    assert json.loads(memory_tool(action="add", target="memory", content="Deploys via make ship on Fridays",
                                  store=s))["success"] is True


def test_default_ceiling_refusal_at_the_hard_cap_never_stages_a_batch(store):
    """M2 regression: the store's cap refusal told the review to retry as a removal batch, which
    was staged instead of applied; every retry left another unreviewed pending record."""
    s = store()
    refused = _review(s, action="add", content="x" * 180)

    _assert_unattended_refusal(refused)
    assert s.memory_entries == [_SEED]
    assert wa.pending_count(wa.MEMORY) == 0


def test_new_text_alias_in_a_batch_is_held_to_the_ceiling(store):
    s = store(ceiling=0.5)
    refused = _review(s, operations=[{"action": "add", "new_text": "y" * 80}])

    _assert_unattended_refusal(refused)
    assert s.memory_entries == [_SEED]


def test_store_overflow_after_the_check_still_gets_the_no_removal_refusal(store, monkeypatch):
    """The ceiling check and the write lock separately; if another writer fills the store in
    between, the store's 'retry as ONE batch' refusal must not reach the unattended review."""
    import tools.memory_tool as mt
    s = store()
    monkeypatch.setattr(mt, "unattended_add_refusal", lambda *a, **k: None)  # the check saw room

    refused = _review(s, action="add", content="x" * 180)

    _assert_unattended_refusal(refused)
    assert "is full for unattended background-review adds" in refused["error"]
    assert wa.pending_count(wa.MEMORY) == 0
    foreground = json.loads(memory_tool(action="add", target="memory", content="x" * 180, store=s))
    assert "Retry as ONE" in foreground["error"]  # foreground keeps the consolidation instruction
