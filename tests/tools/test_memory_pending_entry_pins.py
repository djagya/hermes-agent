"""dlz: staged memory writes are guarded per entry (their ``matched_entry`` pins), not by a
whole-file hash. The fork used to pin a sha256 of the whole target on every staged record, so
any accepted change made every other pending record stale (183 of 183 on the live box)."""

import json
import os
import shutil
import tempfile

import pytest

from hermes_cli.write_approval_commands import handle_pending_subcommand
from tools import write_approval as wa
from tools.memory_tool import load_on_disk_store, memory_tool
from tools.skill_provenance import (reset_current_write_origin, reset_review_attended,
                                    set_current_write_origin, set_review_attended)

_A = "Staging DB: pg-staging-2 (old cluster, retiring)"
_B = "Deploy host: build-01 (decommissioned)"
_C = "Repo lives in ~/src/app; tests via make test"


@pytest.fixture
def hermes_home(monkeypatch):
    root = tempfile.mkdtemp(prefix="hermes_pins_")
    home = os.path.join(root, ".hermes")
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(root, ignore_errors=True)


def _review_stage(store, **kwargs):
    origin, attended = set_current_write_origin("background_review"), set_review_attended(False)
    try:
        staged = json.loads(memory_tool(target="memory", store=store, **kwargs))
    finally:
        reset_review_attended(attended)
        reset_current_write_origin(origin)
    assert staged["staged"] is True, staged
    return staged["pending_id"]


def _seeded_store():
    store = load_on_disk_store()
    for entry in (_A, _B, _C):
        assert store.add("memory", entry)["success"]
    return store


def test_approving_one_record_leaves_records_on_other_entries_applicable(hermes_home):
    store = _seeded_store()
    first = _review_stage(store, action="remove", old_text="Staging DB")
    second = _review_stage(store, operations=[{"action": "remove", "old_text": "Deploy host"}])
    # An unrelated foreground add between staging and review must not stale either record.
    assert store.add("memory", "Timezone: Europe/Berlin")["success"]

    for pid in (first, second):
        out = handle_pending_subcommand(wa.MEMORY, ["approve", pid], memory_store=load_on_disk_store())
        assert "Approved 1" in out, out

    assert load_on_disk_store().memory_entries == [_C, "Timezone: Europe/Berlin"]
    assert wa.pending_count(wa.MEMORY) == 0


def test_record_whose_own_entry_changed_is_still_refused(hermes_home):
    store = _seeded_store()
    pid = _review_stage(store, action="patch", old_text="build-01", content="build-02")
    newer = "Deploy host: build-03 (current)"
    assert store.patch("memory", "build-01 (decommissioned)", "build-03 (current)")["success"]

    out = handle_pending_subcommand(wa.MEMORY, ["approve", pid], memory_store=load_on_disk_store())

    assert "changed since it was staged" in out
    assert load_on_disk_store().memory_entries == [_A, newer, _C]
    assert wa.get_pending(wa.MEMORY, pid) is not None


def test_legacy_record_with_whole_file_guard_applies_by_its_pin(hermes_home):
    """Records staged before this change carry a ``_write_guard`` hash that no longer matches
    any file; they must not be stuck forever."""
    store = _seeded_store()
    pid = _review_stage(store, action="remove", old_text="Staging DB")
    path = wa._pending_dir(wa.MEMORY) / f"{pid}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["_write_guard"] = {"target": "memory", "expected_sha256": "0" * 64}
    record["payload_sha256"] = wa._payload_sha256(record["payload"])
    path.write_text(json.dumps(record), encoding="utf-8")
    assert store.add("memory", "Timezone: Europe/Berlin")["success"]

    out = handle_pending_subcommand(wa.MEMORY, ["approve", pid], memory_store=load_on_disk_store())

    assert "Approved 1" in out, out
    assert _A not in load_on_disk_store().memory_entries
