"""Shared terminal persistence for ordinary completion and attested operator gates."""

from hermes_cli import kanban_db as kb


def record_completion(
    conn, task_id, *, now, source_statuses, result, summary, metadata,
    verified_cards, expected_run_id=None,
):
    """Caller owns the write transaction, eligibility and acceptance checks.

    Return (updated, run_id); a never-claimed task can complete without a run.
    This is persistence, not a public lifecycle transition.
    """
    prior_status = kb._task_status(conn, task_id)
    placeholders = ",".join("?" for _ in source_statuses)
    sql = f"""
        UPDATE tasks
           SET status = 'done', result = ?, completed_at = ?,
               claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
               block_kind = NULL, blocker_key = NULL, block_recurrences = 0
         WHERE id = ? AND status IN ({placeholders})
    """
    params = (result, now, task_id, *source_statuses)
    if expected_run_id is not None:
        sql += " AND current_run_id = ?"
        params = (*params, int(expected_run_id))
    if conn.execute(sql, params).rowcount != 1:
        return False, None
    if isinstance(metadata, dict):
        kb._stage_completion_artifacts(conn, task_id, metadata, now)
    run_id = kb._end_run(
        conn, task_id, outcome="completed", status="done", summary=summary,
        metadata=metadata,
    )
    if run_id is None and (summary or metadata or result or prior_status == "review"):
        synth_summary, synth_metadata = summary, metadata
        if prior_status == "review" and not synth_summary and not synth_metadata:
            synth_summary = kb._REVIEW_APPROVED_NOTE
            synth_metadata = {"source_status": "review", "approval": "manual"}
        run_id = kb._synthesize_ended_run(
            conn, task_id, outcome="completed", summary=synth_summary, metadata=synth_metadata,
        )
    event_summary = summary
    if prior_status == "review" and not event_summary:
        event_summary = kb._REVIEW_APPROVED_NOTE
    kb._append_event(
        conn, task_id, "completed",
        kb._completed_event_payload(result, event_summary, verified_cards, metadata),
        run_id=run_id,
    )
    return True, run_id


def finish_completion(
    conn, task_id, *, run_id, summary, result, verified_cards, fire_lifecycle_hook=True,
):
    """Run native post-commit work only after terminal state is durable."""
    kb._flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)
    kb._clear_failure_counter(conn, task_id)
    kb.recompute_ready(conn)
    kb._cleanup_workspace(conn, task_id)
    done_task = kb.get_task(conn, task_id)
    if fire_lifecycle_hook:
        kb._fire_task_hook("kanban_task_completed", done_task, task_id, run_id, summary=summary)
