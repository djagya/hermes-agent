"""Focused regressions for the two official-core pending-review completion fixes.

1. Atomic skill_manage batch: an operation that RAISES rolls back every
   already-applied snapshot (and snapshot evidence is retained when rollback
   itself fails), not only operations that return a failure result.
2. Reviewed expected_payload_sha256: the claim (apply) and discard boundaries
   verify the caller's reviewed payload digest inside the pending-store lock,
   so a record substituted under the same id is never consumed under an
   earlier decision. Legacy callers without the keyword are unchanged.
"""

import glob
import hashlib
import json
import os
import shutil
import tempfile

import pytest


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_prc_test_")
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


def _make_skill(home, name, text):
    skill_dir = os.path.join(home, "skills", name)
    os.makedirs(skill_dir, exist_ok=True)
    path = os.path.join(skill_dir, "SKILL.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _canonical_payload_sha256(payload):
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stage_memory_add(store, content, target="user"):
    from tools import write_approval as wa
    from tools.memory_tool import _build_memory_write_guard
    return wa.stage_write(
        "memory",
        {
            "action": "add",
            "target": target,
            "content": content,
            "_write_guard": _build_memory_write_guard(store, target),
        },
        summary=content,
        origin="foreground",
    )


# ---------------------------------------------------------------------------
# Fix 1: exception-safe atomic batch rollback
# ---------------------------------------------------------------------------


def test_batch_op_failure_result_rolls_back_earlier_ops(hermes_home):
    from tools import skill_manager_tool as smt

    _set_approval("skills", False)
    alpha = _make_skill(hermes_home, "alpha",
                        "---\nname: alpha\ndescription: d.\n---\nalpha one\n")
    beta = _make_skill(hermes_home, "beta",
                       "---\nname: beta\ndescription: d.\n---\nbeta one\n")
    result = json.loads(smt.skill_manage(
        action="", name="",
        operations=[
            {"action": "patch", "name": "alpha",
             "old_string": "one", "new_string": "two"},
            # Missing required file_content -> failure RESULT after op 1 applied.
            {"action": "write_file", "name": "beta", "file_path": "notes.md"},
        ],
    ))
    assert result["success"] is False
    assert result["failed_index"] == 1
    assert result["completed_before_failure"] == 1
    assert "batch aborted" in result["error"]
    assert "one" in _read(alpha) and "two" not in _read(alpha)
    assert "beta one" in _read(beta)


def test_batch_raised_exception_rolls_back_and_propagates(hermes_home, monkeypatch):
    from tools import skill_manager_tool as smt

    _set_approval("skills", False)
    alpha = _make_skill(hermes_home, "alpha",
                        "---\nname: alpha\ndescription: d.\n---\nalpha one\n")
    _make_skill(hermes_home, "beta",
                "---\nname: beta\ndescription: d.\n---\nbeta one\n")

    original = smt._patch_skill

    def boom(name, old_string, new_string, file_path=None, replace_all=False):
        if name == "beta":
            raise OSError("injected disk failure")
        return original(name, old_string, new_string, file_path, replace_all)

    monkeypatch.setattr(smt, "_patch_skill", boom)

    with pytest.raises(OSError, match="injected disk failure"):
        smt.skill_manage(
            action="", name="",
            operations=[
                {"action": "patch", "name": "alpha",
                 "old_string": "one", "new_string": "two"},
                {"action": "patch", "name": "beta",
                 "old_string": "one", "new_string": "two"},
            ],
        )

    # Op 1's mutation is rolled back, and the batch gate bypass is restored.
    assert "one" in _read(alpha) and "two" not in _read(alpha)
    assert smt._skill_gate_bypass.get() is False


def test_batch_rollback_failure_keeps_snapshot_evidence(hermes_home, monkeypatch):
    from tools import skill_manager_batch as smb
    from tools import skill_manager_tool as smt

    _set_approval("skills", False)
    alpha = _make_skill(hermes_home, "alpha",
                        "---\nname: alpha\ndescription: d.\n---\nalpha one\n")
    _make_skill(hermes_home, "beta",
                "---\nname: beta\ndescription: d.\n---\nbeta one\n")

    original_patch = smt._patch_skill
    original_restore = smb._restore_snapshot
    restore_calls = {"n": 0}

    def boom(name, old_string, new_string, file_path=None, replace_all=False):
        if name == "beta":
            raise OSError("injected disk failure")
        return original_patch(name, old_string, new_string, file_path, replace_all)

    def flaky_restore(pre_dir, snap, post_dir):
        restore_calls["n"] += 1
        if restore_calls["n"] == 1:  # first rollback attempt fails
            raise OSError("injected restore failure")
        return original_restore(pre_dir, snap, post_dir)

    monkeypatch.setattr(smt, "_patch_skill", boom)
    monkeypatch.setattr(smb, "_restore_snapshot", flaky_restore)

    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "skill_batch_*")))
    with pytest.raises(OSError, match="injected disk failure"):
        smt.skill_manage(
            action="", name="",
            operations=[
                {"action": "patch", "name": "alpha",
                 "old_string": "one", "new_string": "two"},
                {"action": "patch", "name": "beta",
                 "old_string": "one", "new_string": "two"},
            ],
        )

    assert restore_calls["n"] >= 1
    # Rollback of 'alpha' failed and must NOT have been silently dropped: the
    # batch keeps its snapshot directory as recovery evidence.
    kept = [d for d in set(glob.glob(os.path.join(tempfile.gettempdir(),
                                                  "skill_batch_*"))) - before
            if os.path.isdir(os.path.join(d, "beta"))]
    assert kept, "snapshot evidence was deleted despite failed rollback"
    for d in kept:  # clean only this test's own scratch
        shutil.rmtree(d, ignore_errors=True)
    # The failed skill stays in its post-failure state; the batch reported the
    # original op exception rather than the rollback failure.
    assert "two" in _read(alpha)


def test_batch_atomic_contract_version_declared():
    from tools import skill_manager_batch as smb

    assert smb.BATCH_ATOMIC_CONTRACT_VERSION == 2


# ---------------------------------------------------------------------------
# Fix 2: reviewed expected_payload_sha256 at the claim/discard boundary
# ---------------------------------------------------------------------------


def test_apply_claim_rejects_substituted_payload_before_claiming(hermes_home):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.load_from_disk()
    record = _stage_memory_add(store, "reviewed entry")

    ok, msg = wa.apply_pending_record(
        wa.MEMORY, record["id"], lambda current: (True, "applied"),
        expected_payload_sha256="0" * 64,
    )
    assert ok is False
    assert "mismatch" in msg
    live = wa.get_pending(wa.MEMORY, record["id"])
    assert live is not None
    assert live.get("state", "pending") == "pending"
    assert store.user_entries == []  # nothing was claimed or applied

    # The exact reviewed digest still applies and consumes the record.
    ok2, msg2 = wa.apply_pending_record(
        wa.MEMORY, record["id"], lambda current: (True, "applied"),
        expected_payload_sha256=record["payload_sha256"],
    )
    assert ok2 is True, msg2
    assert store.user_entries == ["reviewed entry"]
    assert wa.get_pending(wa.MEMORY, record["id"]) is None


def test_apply_claim_rejects_malformed_expected_digest(hermes_home):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.load_from_disk()
    record = _stage_memory_add(store, "entry")
    for bad in ("short", "0x" + "0" * 62, "Z" * 64):
        ok, msg = wa.apply_pending_record(
            wa.MEMORY, record["id"], lambda current: (True, "applied"),
            expected_payload_sha256=bad,
        )
        assert ok is False
        assert "expected_payload_sha256" in msg
    assert wa.get_pending(wa.MEMORY, record["id"]) is not None
    assert wa.discard_pending(wa.MEMORY, record["id"]) is True


def test_discard_with_expected_digest_never_deletes_substitution(hermes_home):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.load_from_disk()
    record = _stage_memory_add(store, "reject-me")
    pending_id = record["id"]

    assert wa.discard_pending(
        wa.MEMORY, pending_id, expected_payload_sha256="1" * 64) is False
    assert wa.get_pending(wa.MEMORY, pending_id) is not None
    assert wa.discard_pending(
        wa.MEMORY, pending_id,
        expected_payload_sha256=record["payload_sha256"]) is True
    assert wa.get_pending(wa.MEMORY, pending_id) is None


def test_same_id_substitution_is_never_claimed_under_reviewed_digest(hermes_home):
    """Audit regression: a schema-valid record swapped in under the same id
    must survive an earlier reviewed decision (apply and reject)."""
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, _build_memory_write_guard

    store = MemoryStore()
    store.load_from_disk()
    record = _stage_memory_add(store, "reviewed entry")
    pending_id = record["id"]
    reviewed_digest = record["payload_sha256"]

    # Build a schema-valid substitution: new payload, recomputed canonical
    # digest, internally valid guard — exactly what a swapped file needs to
    # look legitimate to the pre-existing validations.
    attacker_payload = {
        "action": "add",
        "target": "user",
        "content": "attacker entry",
        "_write_guard": _build_memory_write_guard(store, "user"),
    }
    path = os.path.join(hermes_home, "pending", "memory", f"{pending_id}.json")
    with open(path, encoding="utf-8") as handle:
        swapped = json.load(handle)
    swapped["payload"] = attacker_payload
    swapped["payload_sha256"] = _canonical_payload_sha256(attacker_payload)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(swapped, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    ok, msg = wa.apply_pending_record(
        wa.MEMORY, pending_id, lambda current: (True, "applied"),
        expected_payload_sha256=reviewed_digest,
    )
    assert ok is False
    assert "mismatch" in msg
    assert store.user_entries == []

    out = wa.discard_pending(
        wa.MEMORY, pending_id, expected_payload_sha256=reviewed_digest)
    assert out is False  # rejection decision cannot consume the substitution
    assert wa.get_pending(wa.MEMORY, pending_id) is not None
    # Cleanup of this test's own scratch store.
    assert wa.discard_pending(wa.MEMORY, pending_id) is True


def test_bound_official_skill_apply_succeeds_unchanged(hermes_home):
    from tools import skill_manager_tool as smt
    from tools import write_approval as wa

    skill_md = _make_skill(
        hermes_home, "demo",
        "---\nname: demo\ndescription: Use when testing binding.\nversion: 1.0.0\n---\nold\n")
    record = smt.stage_skill_write(
        {"action": "patch", "name": "demo",
         "old_string": "old", "new_string": "new"},
        summary="patch demo", origin="foreground",
    )

    def apply_current(current):
        result = json.loads(smt.apply_skill_pending(current["payload"]))
        return bool(result.get("success")), result.get("error", "")

    ok, msg = wa.apply_pending_record(
        wa.SKILLS, record["id"], apply_current,
        expected_payload_sha256=record["payload_sha256"],
    )
    assert ok is True, msg
    assert "new" in _read(skill_md)
    assert wa.get_pending(wa.SKILLS, record["id"]) is None


def test_legacy_callers_without_keyword_are_unchanged(hermes_home):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.load_from_disk()
    record = _stage_memory_add(store, "legacy apply")
    ok, msg = wa.apply_pending_record(
        wa.MEMORY, record["id"], lambda current: (True, "applied"))
    assert ok is True, msg
    assert store.user_entries == ["legacy apply"]

    record2 = _stage_memory_add(store, "legacy discard")
    assert wa.discard_pending(wa.MEMORY, record2["id"]) is True
    assert wa.get_pending(wa.MEMORY, record2["id"]) is None


# ---------------------------------------------------------------------------
# Handler surface (hermes_cli.write_approval_commands)
# ---------------------------------------------------------------------------


def test_handle_approve_all_with_digest_is_rejected(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.load_from_disk()
    record = _stage_memory_add(store, "entry")
    out = handle_pending_subcommand(
        wa.MEMORY, ["approve", "all"], memory_store=store,
        expected_payload_sha256=record["payload_sha256"],
    )
    assert "cannot be combined" in out
    assert wa.get_pending(wa.MEMORY, record["id"]) is not None
    wa.discard_pending(wa.MEMORY, record["id"])


def test_handle_bound_approve_and_reject(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.load_from_disk()
    apply_record = _stage_memory_add(store, "bound apply")
    out = handle_pending_subcommand(
        wa.MEMORY, ["approve", apply_record["id"]], memory_store=store,
        expected_payload_sha256=apply_record["payload_sha256"],
    )
    assert "Approved 1" in out
    assert store.user_entries == ["bound apply"]

    reject_record = _stage_memory_add(store, "bound reject")
    out = handle_pending_subcommand(wa.MEMORY, ["reject", reject_record["id"]])
    assert f"Rejected pending memory write '{reject_record['id']}'" in out
    assert wa.pending_count(wa.MEMORY) == 0


def test_handle_bound_approve_mismatch_reports_without_applying(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.load_from_disk()
    record = _stage_memory_add(store, "entry")
    out = handle_pending_subcommand(
        wa.MEMORY, ["approve", record["id"]], memory_store=store,
        expected_payload_sha256="0" * 64,
    )
    assert "Approved 0" in out
    assert "mismatch" in out
    assert store.user_entries == []
    live = wa.get_pending(wa.MEMORY, record["id"])
    assert live is not None and live.get("state", "pending") == "pending"
    wa.discard_pending(wa.MEMORY, record["id"])
