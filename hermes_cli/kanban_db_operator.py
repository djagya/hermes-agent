"""Trusted-operator triage transitions; attestations are audit, not authentication.

No worker tool forwards these APIs. Same-UID Python or SQL access is not isolated
by this boundary; operators must reconcile process ownership outside the database.
"""

import copy
import json
import os
import time

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_completion import finish_completion, record_completion
from hermes_cli.kanban_db_recovery import _snapshot, recovery_snapshot


def require_operator_context():
    from agent.delegation_context import is_delegated_child_process_context

    if (is_delegated_child_process_context()
            or os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_RUN_ID")):
        raise PermissionError("operator recovery is unavailable in worker/child contexts")


def _attestation(*, expected_fingerprint, author, reason, operator, process_quiescent):
    require_operator_context()
    if not all(isinstance(value, str) and value.strip()
               for value in (expected_fingerprint, author, reason)):
        raise ValueError("fingerprint, author and reason must be nonempty strings")
    if operator is not True or process_quiescent is not True:
        raise ValueError("explicit operator and reconciled process-ownership attestations required")
    return {"author": author, "reason": reason, "before_fingerprint": expected_fingerprint,
            "operator": True, "process_quiescent": True}


def _eligible(snapshot, expected_fingerprint):
    task = snapshot["task"]
    return (
        snapshot["fingerprint"] == expected_fingerprint and task["status"] == "triage"
        and all(task[field] is None for field in (
            "claim_lock", "claim_expires", "worker_pid", "current_run_id",
        ))
        and not any(run["ended_at"] is None or run["status"] == "running"
                    for run in snapshot["runs"])
    )


def _current(conn, task_id, fingerprint):
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        return None
    snapshot = _snapshot(conn, task_id)
    return snapshot if _eligible(snapshot, fingerprint) else None


def resolve_triage_blocker(
    conn, task_id, *, expected_fingerprint, author, reason, operator,
    process_quiescent, role, resolved_blocker, decision, evidence_reference,
):
    """Resolve exactly the attested typed event, preserving history and retry counters.

    This decision does not grant action authority or resolve another hold. Executable
    work lands in todo; native readiness restores review when appropriate and waits
    for parents. A repeated request cannot apply to an already recovered task.
    """
    audit = _attestation(
        expected_fingerprint=expected_fingerprint, author=author, reason=reason,
        operator=operator, process_quiescent=process_quiescent,
    )
    if role != "executable":
        raise ValueError("explicit executable role required; gates cannot be resumed")
    if not isinstance(resolved_blocker, dict) or not resolved_blocker:
        raise ValueError("exact typed blocker event required")
    if not all(isinstance(value, str) and value.strip() for value in (decision, evidence_reference)):
        raise ValueError("explicit resolution decision and evidence reference required")
    with kb.write_txn(conn):
        snapshot = _current(conn, task_id, expected_fingerprint)
        if snapshot is None or snapshot["blocker"] != resolved_blocker:
            return False
        for event in snapshot["events"]:
            if event["kind"] == "typed_blocker_resolved":
                prior = kb._json_dict(event["payload"]).get("resolved_blocker")
                if isinstance(prior, dict) and prior.get("id") == resolved_blocker.get("id"):
                    return False
        try:
            payload = json.loads(resolved_blocker["payload"] or "{}")
        except (ValueError, TypeError, KeyError):
            return False
        if not isinstance(payload, dict) or payload.get("kind") not in kb.VALID_BLOCK_KINDS:
            return False
        if (snapshot["task"]["block_kind"] != payload["kind"]
                or snapshot["task"]["blocker_key"] != payload.get("blocker_key")):
            return False
        resume_status = kb._resume_status_from_events(conn, task_id)
        # As with native unblock, retain cause/recurrences to avoid retry amnesia.
        conn.execute(
            "UPDATE tasks SET status = 'todo', consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?", (task_id,),
        )
        kb._append_event(conn, task_id, "typed_blocker_resolved", {
            **audit, "role": role, "resolved_blocker": resolved_blocker,
            "decision": decision, "evidence_reference": evidence_reference,
            "resume_status": resume_status,
        })
    return True


def complete_triage_gate(
    conn, task_id, *, expected_fingerprint, author, reason, operator,
    process_quiescent, role, nonspawning, summary, metadata,
):
    """Direct triage -> done for an attested nonspawning evidence gate.

    No intermediate runnable status, no title/assignee heuristics, no force flag.
    Completion evidence is required but is not itself a fabricated acceptance
    receipt: PR contracts still collect and check independent native acceptance.
    Typed holds are not discharged by this operation.
    """
    audit = _attestation(
        expected_fingerprint=expected_fingerprint, author=author, reason=reason,
        operator=operator, process_quiescent=process_quiescent,
    )
    if role != "operator-evidence-gate" or nonspawning is not True:
        raise ValueError("explicit nonspawning operator-evidence-gate role required")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("completion summary required")
    if (not isinstance(metadata, dict) or not isinstance(metadata.get("evidence"), dict)
            or not metadata["evidence"]):
        raise ValueError("nonempty structured metadata.evidence required")
    metadata = copy.deepcopy(metadata)
    # Snapshot before external acceptance collection; recheck after collection while
    # holding the writer lock. Never hold a DB lock during network I/O.
    try:
        before = recovery_snapshot(conn, task_id)
    except ValueError:
        return False
    if not _gate_eligible(before, expected_fingerprint):
        return False
    from hermes_cli.kanban_pr_acceptance import _PR, collect_acceptance
    from hermes_cli.kanban_pr_acceptance_store import record_acceptance
    contract = before["task"]["completion_contract"]
    published_pr = metadata.get("published_pr")
    receipt = None
    if contract and contract != "local-only":
        receipt = collect_acceptance(contract, published_pr)
    with kb.write_txn(conn):
        snapshot = _current(conn, task_id, expected_fingerprint)
        if snapshot is None or not _gate_eligible(snapshot, expected_fingerprint):
            return False
        if receipt is not None:
            accepted = record_acceptance(conn, task_id, ((None, "triage", contract), receipt))
            # Preserve native bind-once semantics even when CI is not yet green:
            # retrying cannot replace a failing PR with an unrelated green sibling.
            match = _PR.fullmatch(published_pr) if isinstance(published_pr, str) else None
            if match and contract == match[1]:
                conn.execute("UPDATE tasks SET completion_contract = ? WHERE id = ?",
                             (published_pr, task_id))
            if not accepted:
                return False
        kb._append_event(conn, task_id, "operator_gate_completed", {
            **audit, "role": role, "nonspawning": True, "evidence": metadata["evidence"],
        })
        metadata = kb._merge_completion_prose_artifacts(
            conn, task_id, metadata, summary=summary, result=None,
        )
        updated, run_id = record_completion(
            conn, task_id, now=int(time.time()), source_statuses=("triage",),
            result=None, summary=summary, metadata=metadata, verified_cards=[],
        )
        if not updated:
            raise RuntimeError("gate changed inside completion transaction")
    finish_completion(conn, task_id, run_id=run_id, summary=summary, result=None, verified_cards=[])
    return True


def _gate_eligible(snapshot, fingerprint):
    blocker = snapshot["blocker"]
    if blocker is not None:
        try:
            payload = json.loads(blocker["payload"] or "{}")
        except (ValueError, TypeError, KeyError):
            return False
        if not isinstance(payload, dict) or payload.get("kind") is not None:
            return False
    return (
        _eligible(snapshot, fingerprint)
        and snapshot["task"]["block_kind"] is None
        and not snapshot["task"]["goal_mode"]
        and all(parent["status"] in ("done", "archived") for parent in snapshot["parents"])
    )
