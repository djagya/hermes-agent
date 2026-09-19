"""Truncation-integrity invariants.

Production incident (2026-09-19, board ``internal``): the send-path context
compressor replaced oversized historical tool-call arguments with a 200-char head
plus the literal ``...[truncated]`` INSIDE otherwise executable JSON. A model on a
long session copied that preview into a FRESH ``kanban_create`` call, which was then
faithfully executed and persisted as an incomplete execution contract (tasks
``t_e7a190fc`` / ``t_d6569a19``; assistant rows 3014312 / 3014314).

Invariants under test (truncation-integrity spec):

1. Compacted historical arguments become a typed opaque reference
   (``hermes_compacted_tool_arguments``) — never a partial payload that looks
   executable. Send-path only; the persisted history keeps the original bytes.
2. A fresh state-bearing payload carrying preview corruption is refused BEFORE any
   side effect, with a typed, secret-safe diagnostic. Read-only query fields keep
   the literal marker searchable — no global word ban.
3. The persist-before-execute boundary verifies, per dispatched tool-call id, that
   the arguments about to run are byte-exact with the persisted assistant row.

These are behaviour contracts between components (compressor ↔ dispatch ↔ store),
not snapshots; local-only — Hermes suite execution is CI/isolated per spec.
"""

import copy
import hashlib
import json

import pytest
from unittest.mock import patch

from agent.context_compressor import ContextCompressor, _truncate_tool_call_args_json
from tools.integrity_guard import (
    COMPACTED_ARGS_KIND,
    TRUNCATION_PREVIEW_SENTINEL,
    find_corrupted_payload,
    guard_effectful_payload,
)
from agent.tool_payload_integrity import verify_persist_before_execute


def _large_args(tool_payload_chars: int = 2000) -> str:
    return json.dumps({"title": "Spec task", "assignee": "worker-a",
                       "body": "canonical bytes " * (tool_payload_chars // 16)})


# ── Invariant 1: historical compaction is typed + opaque + send-path only ───────────


class TestHistoricalCompactionIsOpaque:
    def test_reference_carries_name_length_sha_and_non_executable(self):
        original = _large_args()
        ref = json.loads(_truncate_tool_call_args_json(original, tool_name="kanban_create"))
        assert ref["kind"] == COMPACTED_ARGS_KIND
        assert ref["tool"] == "kanban_create"
        assert ref["original_length"] == len(original)
        assert ref["sha256"] == hashlib.sha256(original.encode()).hexdigest()
        assert ref["non_executable"] is True
        assert "durable session history" in ref["instruction"]

    def test_no_partial_payload_escapes(self):
        original = _large_args()
        sent = _truncate_tool_call_args_json(original, tool_name="kanban_create")
        assert TRUNCATION_PREVIEW_SENTINEL not in sent
        assert "canonical bytes" not in sent

    def test_compaction_reduces_and_stays_bounded_at_scale(self):
        small_ref = _truncate_tool_call_args_json(_large_args(), tool_name="kanban_create")
        huge_ref = _truncate_tool_call_args_json(
            json.dumps({"body": "y" * 500_000}), tool_name="kanban_create")
        assert len(small_ref) < 600 and len(huge_ref) < 600

    def test_small_and_unparseable_args_untouched(self):
        small = json.dumps({"path": "a.txt"})
        assert _truncate_tool_call_args_json(small) == small
        broken = '{"content": "cut'
        assert _truncate_tool_call_args_json(broken) == broken

    def test_write_file_history_becomes_reference_without_copyable_prefix(self):
        # Live-reproducer acceptance (2026-09-19): a compacted write_file document must
        # carry NO executable prefix of the original content — nothing copyable remains,
        # only the typed reference.
        content = "intended PR body " * 400
        original = json.dumps({"path": "/tmp/pr.md", "content": content})
        sent = _truncate_tool_call_args_json(original, tool_name="write_file")
        ref = json.loads(sent)
        assert ref["kind"] == COMPACTED_ARGS_KIND and ref["non_executable"] is True
        assert ref["tool"] == "write_file" and ref["original_length"] == len(original)
        assert "PR body" not in sent and TRUNCATION_PREVIEW_SENTINEL not in sent
        assert original == json.dumps({"path": "/tmp/pr.md", "content": content})

    def test_prune_pass_leaves_persisted_history_byte_exact(self):
        with patch("agent.context_compressor.get_model_context_length",
                   return_value=100000):
            c = ContextCompressor(model="test/model", threshold_percent=0.85,
                                  protect_first_n=1, protect_last_n=1, quiet_mode=True)
        original = _large_args()
        messages = [
            {"role": "user", "content": "create the spec task"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "kanban_create", "arguments": original}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"task_id": "t_new"}'},
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        snapshot = copy.deepcopy(messages)
        result, _ = c._prune_old_tool_results(messages, protect_tail_count=2)
        # Send-path copy carries the reference...
        sent = json.loads(result[1]["tool_calls"][0]["function"]["arguments"])
        assert sent["kind"] == COMPACTED_ARGS_KIND
        # ...pairing survives for the provider...
        assert result[1]["role"] == "assistant" and result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "call_1"
        # ...and the persisted history keeps the original bytes verbatim.
        assert messages == snapshot


# ── Invariant 2: effectful refusal before side effects, read-only stays usable ──────


class TestEffectfulRefusal:
    @pytest.mark.parametrize("tool,payload", [
        ("kanban_create", {"title": "t", "assignee": "w",
                           "body": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("kanban_create", {"title": "Long title " * 20 + TRUNCATION_PREVIEW_SENTINEL,
                           "assignee": "w", "body": "clean"}),
        ("kanban_comment", {"task_id": "t_x",
                            "body": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("write_file", {"path": "/tmp/x.md",
                        "content": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("send_message", {"target": "telegram",
                          "message": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("cronjob_manage", {"action": "create", "schedule": "in 10m",
                            "prompt": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("patch", {"path": "/tmp/x.md", "old_string": "a", "new_string":
                   "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
    ])
    def test_state_bearing_fields_refused(self, tool, payload):
        refusal = guard_effectful_payload(tool, payload)
        assert refusal is not None
        assert "compacted_payload_refused" in refusal
        assert "NOT authoritative" in refusal

    def test_other_preview_sentinels_refused_in_state_fields(self):
        # Bare skill-prune placeholder and the persisted-output recovery block are
        # also context-reduction artifacts; a state field carrying them is corrupt.
        assert guard_effectful_payload(
            "kanban_comment", {"task_id": "t", "body": "[SKILL_PRUNED]"}) is not None
        assert guard_effectful_payload(
            "kanban_comment",
            {"task_id": "t", "body": "[SKILL_PRUNED: memory] content lost"}) is not None
        assert guard_effectful_payload(
            "write_file", {"path": "/tmp/x.md", "content":
                           "<persisted-output>path=/tmp/o.txt</persisted-output>"}) is not None

    def test_typed_reference_refused_wherever_it_appears(self):
        ref = {"kind": COMPACTED_ARGS_KIND, "tool": "kanban_create",
               "sha256": "0" * 64, "non_executable": True}
        top = find_corrupted_payload("kanban_create", ref)
        assert top is not None and top["reason"] == "compacted_tool_arguments_reference"
        embedded = find_corrupted_payload(
            "kanban_create", {"title": "t", "assignee": "w", "body": json.dumps(ref)})
        assert embedded is not None
        # The reference is never legitimate command content either.
        assert guard_effectful_payload(
            "terminal", {"command": f"kanban_create {json.dumps(ref)}"}) is not None

    def test_no_execution_on_refused_create(self):
        from model_tools import handle_function_call
        corrupted = {"title": "t", "assignee": "w",
                     "body": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}
        result = handle_function_call("kanban_create", corrupted)
        assert "compacted_payload_refused" in result

    def test_read_only_tools_keep_marker_searchable(self):
        assert guard_effectful_payload(
            "search_files", {"pattern": TRUNCATION_PREVIEW_SENTINEL}) is None
        assert guard_effectful_payload(
            "web_search", {"query": TRUNCATION_PREVIEW_SENTINEL}) is None
        # Free-form command probing the SHORT literal stays allowed...
        assert guard_effectful_payload(
            "terminal", {"command": f"grep -r '{TRUNCATION_PREVIEW_SENTINEL}' logs/"}) is None
        assert guard_effectful_payload(
            "terminal",
            {"command": f"cat {TRUNCATION_PREVIEW_SENTINEL} >> notes.txt"}) is None
        # ...but the full corruption fingerprint (long head + sentinel) refuses.
        bad = "cat > out.md <<'EOF'\n" + "head " * 60 + TRUNCATION_PREVIEW_SENTINEL + "\nEOF"
        assert guard_effectful_payload("terminal", {"command": bad}) is not None

    def test_large_clean_payload_still_executes(self):
        assert guard_effectful_payload(
            "kanban_create", {"title": "t", "assignee": "w", "body": "x" * 50_000}) is None

    def test_live_reproducer_write_file_refused_before_side_effect(self, tmp_path):
        # Live reproducer (2026-09-19, PR #31 prep): a long write_file call carried the
        # compressor preview in its content and a real 211-byte file landed. Acceptance:
        # a fresh effectful write carrying preview corruption fails TYPED, before any
        # side effect — the target file must never exist.
        target = tmp_path / "should-never-exist.md"
        corrupted = {"path": str(target), "content":
                     "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}
        from model_tools import handle_function_call
        result = handle_function_call("write_file", dict(corrupted))
        assert "compacted_payload_refused" in result
        assert not target.exists()

    def test_nested_bridge_and_batch_entries_classified(self):
        corrupted = {"title": "t", "assignee": "w",
                     "body": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}
        nested = find_corrupted_payload(
            "tool_call", {"name": "kanban_create", "arguments": corrupted})
        assert nested is not None
        batch = find_corrupted_payload("tool_call", {"calls": [
            {"name": "kanban_comment",
             "arguments": {"task_id": "t", "body": corrupted["body"]}},
            {"name": "web_search", "arguments": {"query": "clean"}},
        ]})
        assert batch is not None
        assert batch["field"].startswith("calls[0]")
        assert "calls[1]" not in batch["field"]

    def test_diagnostic_is_typed_and_secret_safe(self):
        secret_body = "head " * 60 + TRUNCATION_PREVIEW_SENTINEL + " token=«redacted:sk-…»"
        finding = find_corrupted_payload(
            "kanban_create", {"title": "t", "assignee": "w", "body": secret_body})
        assert finding["field"] == "body"
        assert len(finding["sha256"]) == 64
        blob = json.dumps(finding)
        assert "«redacted:sk-…»" not in blob

    def test_read_only_classification_derived_from_canonical_set(self):
        # Contract between the guard and the canonical effect classification (spec §C,
        # round-1 review finding 2): the guard's read-only set CONTAINS the canonical
        # no-effect set and adds only the kanban/toolset read surface — it never runs a
        # parallel copy that can drift stale.
        from agent.tool_result_classification import NO_EFFECT_TOOL_NAMES
        from tools.integrity_guard import READ_ONLY_TOOL_NAMES
        assert NO_EFFECT_TOOL_NAMES <= READ_ONLY_TOOL_NAMES
        extras = READ_ONLY_TOOL_NAMES - NO_EFFECT_TOOL_NAMES
        assert extras == {"tool_search", "tool_describe", "kanban_show",
                          "kanban_attachments", "kanban_list"}


# ── Invariant 3: persist-before-execute byte-exactness (per tool-call id) ───────────


class TestPersistBeforeExecuteInvariant:
    def _row(self, *calls):
        return {"role": "assistant", "tool_calls": [
            {"id": cid, "function": {"name": name, "arguments": args}}
            for cid, name, args in calls]}

    def test_intact_envelope_passes(self):
        args = _large_args()
        row = self._row(("c1", "kanban_create", args))
        assert verify_persist_before_execute(
            row, parsed_calls=[{"id": "c1", "name": "kanban_create", "arguments": args}]
        ) is None

    def test_row_without_calls_passes(self):
        assert verify_persist_before_execute(
            {"role": "assistant"}, parsed_calls=[]) is None

    def test_row_vs_handler_divergence_stops_the_turn(self):
        args = _large_args()
        row = self._row(("c1", "kanban_create", args))
        failure = verify_persist_before_execute(
            row,
            parsed_calls=[{"id": "c1", "name": "kanban_create", "arguments": args + " "}])
        assert failure is not None
        assert failure["mismatches"][0]["boundary"] == "assistant_row_vs_handler"
        assert failure["mismatches"][0]["call_id"] == "c1"

    def test_mixed_batch_extra_row_entries_do_not_misalign(self):
        # The row keeps EVERY emitted call (invalid ones too) while only valid calls
        # dispatch. Positional pairing would compare c1 against c_invalid and stop
        # the turn spuriously; id-keyed pairing must pass.
        good = _large_args()
        row = self._row(("c_invalid", "bad_tool", "{}"), ("c1", "kanban_create", good))
        assert verify_persist_before_execute(
            row,
            parsed_calls=[{"id": "c1", "name": "kanban_create", "arguments": good}]
        ) is None

    def test_pending_preview_corruption_refused_at_boundary(self):
        row = self._row()
        bad = "cat > out.md <<'EOF'\n" + "head " * 60 + TRUNCATION_PREVIEW_SENTINEL + "\nEOF"
        failure = verify_persist_before_execute(
            row,
            parsed_calls=[{"id": "c1", "name": "terminal",
                           "arguments": json.dumps({"command": bad})}])
        assert failure is not None
        assert any(m["boundary"] == "pending_call_preview_corruption"
                   for m in failure["mismatches"])

    # The corruption leg shares the dispatcher's effect/field classification
    # (tools.integrity_guard) — spec section C: read-only tools stay usable for the
    # literal marker; no global word ban (round-1 review finding 1).

    def test_read_only_marker_query_passes_the_boundary(self):
        failure = verify_persist_before_execute(
            self._row(),
            parsed_calls=[{"id": "c9", "name": "search_files",
                           "arguments": json.dumps({"pattern": TRUNCATION_PREVIEW_SENTINEL})}])
        assert failure is None

    def test_web_search_marker_query_passes_the_boundary(self):
        failure = verify_persist_before_execute(
            self._row(),
            parsed_calls=[{"id": "c9", "name": "web_search",
                           "arguments": json.dumps({"query": TRUNCATION_PREVIEW_SENTINEL})}])
        assert failure is None

    def test_short_literal_in_command_passes_the_boundary(self):
        failure = verify_persist_before_execute(
            self._row(),
            parsed_calls=[{"id": "c1", "name": "terminal",
                           "arguments": json.dumps(
                               {"command": f"grep -r '{TRUNCATION_PREVIEW_SENTINEL}' logs/"})}])
        assert failure is None

    @pytest.mark.parametrize("name,args", [
        ("kanban_create", {"title": "t", "assignee": "w",
                           "body": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("kanban_comment", {"task_id": "t",
                            "body": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("write_file", {"path": "/tmp/x",
                        "content": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        # Legacy alias must classify too: the dispatcher canonicalizes before guarding.
        ("cronjob", {"action": "create", "schedule": "in 10m",
                     "prompt": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
    ])
    def test_state_bearing_pending_calls_refused_at_boundary(self, name, args):
        failure = verify_persist_before_execute(
            self._row(),
            parsed_calls=[{"id": "c1", "name": name, "arguments": json.dumps(args)}])
        assert failure is not None
        m = failure["mismatches"][0]
        assert m["boundary"] == "pending_call_preview_corruption"
        assert m["reason"] == "truncation_sentinel"
        assert "head " not in json.dumps(m)  # diagnostic stays secret-safe

    def test_fingerprint_only_pending_code_refused(self):
        bad = "x = '" + "head " * 60 + TRUNCATION_PREVIEW_SENTINEL + "'"
        failure = verify_persist_before_execute(
            self._row(),
            parsed_calls=[{"id": "c1", "name": "execute_code",
                           "arguments": json.dumps({"code": bad})}])
        assert failure is not None

    def test_typed_reference_pending_call_refused(self):
        ref = {"kind": COMPACTED_ARGS_KIND, "tool": "kanban_create",
               "sha256": "0" * 64, "non_executable": True}
        failure = verify_persist_before_execute(
            self._row(),
            parsed_calls=[{"id": "c1", "name": "kanban_create",
                           "arguments": json.dumps(ref)}])
        assert failure is not None
        assert failure["mismatches"][0]["reason"] == "compacted_tool_arguments_reference"

    @pytest.mark.parametrize("name,args", [
        ("kanban_create", {"title": "t", "assignee": "w",
                           "body": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("cronjob", {"action": "create",
                     "prompt": "head " * 60 + TRUNCATION_PREVIEW_SENTINEL}),
        ("terminal", {"command": "cat > out.md <<'EOF'\n" + "head " * 60
                      + TRUNCATION_PREVIEW_SENTINEL + "\nEOF"}),
        ("search_files", {"pattern": TRUNCATION_PREVIEW_SENTINEL}),
    ])
    def test_boundary_never_disagrees_with_dispatcher(self, name, args):
        # Same payload at both seams: refused at dispatch  <=>  flagged at the boundary.
        from model_tools import handle_function_call
        refusal = handle_function_call(name, json.loads(json.dumps(args)))
        failure = verify_persist_before_execute(
            self._row(),
            parsed_calls=[{"id": "c1", "name": name, "arguments": json.dumps(args)}])
        refused_at_dispatch = "compacted_payload_refused" in refusal
        flagged_at_boundary = failure is not None and any(
            m["boundary"] == "pending_call_preview_corruption"
            for m in failure["mismatches"])
        assert refused_at_dispatch == flagged_at_boundary


# ── Durable-source handoff contract (spec section E) ─────────────────────────────────


class TestDurableSourceContract:
    def test_contract_text_round_trips_through_extraction(self, tmp_path):
        from tools.durable_source import (
            contract_text, extract_digest_contract, referenced_artifact_path,
            sha256_text, verify_bytes,
        )
        payload = "canonical bytes " * 400
        artifact = tmp_path / "spec.md"
        artifact.write_text(payload, encoding="utf-8")
        body = contract_text(payload, artifact)
        contract = extract_digest_contract(body)
        assert contract is not None
        assert contract["sha256"] == sha256_text(payload)
        assert contract["length"] == len(payload.encode())
        assert referenced_artifact_path(body) == str(artifact)
        # Read-back verification fails closed on any byte drift.
        assert verify_bytes(
            artifact.read_bytes(), sha256=contract["sha256"], length=contract["length"]
        )["intact"]
        assert not verify_bytes(
            b"drifted", sha256=contract["sha256"], length=contract["length"]
        )["intact"]

    def test_digest_mismatch_raises(self, tmp_path):
        from tools.durable_source import verify_spec_digest
        f = tmp_path / "a.md"
        f.write_text("real", encoding="utf-8")
        with pytest.raises(ValueError):
            verify_spec_digest(f, "0" * 64)
