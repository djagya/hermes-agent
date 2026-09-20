"""Explicit, single-task legacy acceptance recovery for trusted board operators.

These APIs take an already selected board connection; they never resolve ambient
board defaults. Attestations are audit evidence, not authentication.
"""

import hashlib
import json
import sqlite3

from hermes_cli import kanban_db as kb

_BLOCK_EVENTS = ("blocked", "block_loop_detected", "gave_up", "dependency_wait")


def _snapshot(conn: sqlite3.Connection, task_id: str) -> dict:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise ValueError("task does not exist")
    events = [dict(row) for row in conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,),
    )]
    blockers = [event for event in events if event["kind"] in _BLOCK_EVENTS]
    graph = {}
    for direction, own, other in (
        ("parents", "child_id", "parent_id"),
        ("children", "parent_id", "child_id"),
    ):
        graph[direction] = [dict(row) for row in conn.execute(
            f"SELECT l.parent_id, l.child_id, t.* FROM task_links l "
            f"LEFT JOIN tasks t ON t.id = l.{other} "
            f"WHERE l.{own} = ? ORDER BY l.{other}", (task_id,),
        )]
    state = {
        "version": 1,
        "task": dict(task),
        **graph,
        "events": events,
        "runs": [dict(row) for row in conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,),
        )],
        "blocker": blockers[-1] if blockers else None,
    }
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(), **state}


def recovery_snapshot(conn: sqlite3.Connection, task_id: str) -> dict:
    """Read one consistent snapshot without writing rows or acquiring a writer lock.

    Fingerprints are opaque, versioned optimistic-concurrency tokens. Full task,
    neighboring task, run and event rows intentionally invalidate conservatively.
    The caller must retain the exact ``blocker`` object alongside the fingerprint.
    """
    if conn.in_transaction:
        raise RuntimeError("recovery_snapshot requires its own read transaction")
    conn.execute("BEGIN")
    try:
        return _snapshot(conn, task_id)
    finally:
        conn.rollback()


def recover_triage_task(
    conn: sqlite3.Connection, task_id: str, *, expected_fingerprint: str,
    author: str, reason: str, role: str, legacy_acceptance: bool,
    resolved_blocker: dict,
) -> bool:
    """Atomically accept a reviewed legacy spec, without rewriting it.

    Mandatory operator declarations: ``role='executable'``,
    ``legacy_acceptance=True``, and the snapshot's exact non-null blocker.
    Invalid arguments raise ValueError; stale/ineligible state returns False.
    Both refusal paths write no rows. A successful operation leaves ``todo``;
    the normal dispatcher readiness pass alone decides ready/review eligibility.
    No process absence is inferred from database fields.
    """
    if not all(isinstance(value, str) and value.strip()
               for value in (expected_fingerprint, author, reason)):
        raise ValueError("fingerprint, author and reason must be nonempty strings")
    if role != "executable" or legacy_acceptance is not True:
        raise ValueError("explicit executable role and legacy acceptance are required")
    if not isinstance(resolved_blocker, dict) or not resolved_blocker:
        raise ValueError("exact resolved blocker is required")
    with kb.write_txn(conn):
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            return False
        snapshot = _snapshot(conn, task_id)
        task = snapshot["task"]
        if snapshot["fingerprint"] != expected_fingerprint:
            return False
        if snapshot["blocker"] != resolved_blocker:
            return False
        if task["status"] != "triage" or task["block_kind"] == "needs_input":
            return False
        if any(task[field] is not None for field in (
            "claim_lock", "claim_expires", "worker_pid", "current_run_id",
        )):
            return False
        if any(run["ended_at"] is None or run["status"] == "running"
               for run in snapshot["runs"]):
            return False
        try:
            payload = json.loads(resolved_blocker["payload"] or "{}")
        except (ValueError, TypeError, KeyError):
            return False
        if not isinstance(payload, dict) or payload.get("kind") == "needs_input":
            return False
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        kb._append_event(conn, task_id, "triage_recovered", {
            "author": author, "reason": reason,
            "before_fingerprint": expected_fingerprint,
            "resolved_blocker": resolved_blocker,
            "role": role, "legacy_acceptance": True,
        })
    return True
