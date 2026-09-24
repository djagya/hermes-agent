"""Run-scoped retirement for dispatcher-spawned Kanban workers.

A worker's terminal state belongs to its pinned ``(task, run)``, not to a tool name in
history: a successful complete/block/review/changes outcome (or loss of the run to a
reclaim/successor) ends that run in ``task_runs``. Once ended, the worker must not
dispatch further tool work — including trailing calls in the same batch. A failed
lifecycle call leaves the run open, so repair guidance and tools stay available.

This governs NEW-work admission only. Processes the worker already started keep running;
workspace reuse by a successor is gated separately by the dispatcher
(``hermes_cli.kanban_db_workspace_owners``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

RETIRED_STATUSES = frozenset({"done", "review", "changes_requested", "blocked", "superseded"})

# Lifecycle tools whose successful outcome ends the pinned run. They are a concurrency
# barrier inside the tool executor: a batch sibling must not have its admission check
# pass in parallel before the handoff commits.
TERMINAL_LIFECYCLE_TOOLS = frozenset({
    "kanban_complete", "kanban_request_review", "kanban_request_changes", "kanban_block",
})

# Tools that stay admissible while ownership is UNKNOWN (DB unreadable): pure reads and the
# lifecycle tools, whose handlers enforce the run guard themselves. After retirement
# nothing is admitted.
_UNKNOWN_OWNERSHIP_ALLOWED_PREFIX = "kanban_"


@dataclass(frozen=True)
class RunState:
    """``live`` | ``retired`` | ``unknown`` for one pinned run; ``status`` is the
    run-scoped lifecycle status when known, ``detail`` a diagnostic for ``unknown``."""

    state: str
    task_id: str
    run_id: int
    status: Optional[str] = None
    detail: str = ""


def worker_run_identity() -> Optional[tuple[str, int]]:
    """``(task_id, run_id)`` for a dispatcher-owned worker, else ``None``.

    ``None`` means no pinned run (ordinary sessions, delegate children, manual runs without
    a run id): callers keep their pre-existing behaviour.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if not task_id or not raw_run:
        return None
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        if not is_dispatcher_owned_worker_context():
            return None
        return task_id, int(raw_run)
    except ValueError:
        return None


def read_run_state(task_id: str, run_id: int) -> RunState:
    """Authoritative state of one run from the board DB (``goal_run_status`` owns the
    run-scoped status mapping). Any failure to read is ``unknown``, never ``live``."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            status = kb.goal_run_status(conn, task_id, run_id)
    except Exception as exc:  # DB missing/locked/corrupt: ownership cannot be proven
        logger.debug("kanban run state unreadable for %s/%s", task_id, run_id, exc_info=True)
        return RunState("unknown", task_id, run_id, detail=f"{type(exc).__name__}: {exc}")
    if status is None:
        return RunState("unknown", task_id, run_id, detail="task not found on the board")
    if status in RETIRED_STATUSES:
        return RunState("retired", task_id, run_id, status=status)
    return RunState("live", task_id, run_id, status=status)


def current_run_state(agent) -> Optional[RunState]:
    """This worker's run state; ``retired`` is sticky on ``agent`` because an ended run
    never reopens. ``None`` when the process has no pinned run."""
    identity = worker_run_identity()
    if identity is None:
        return None
    cached = getattr(agent, "_kanban_run_retired", None)
    if isinstance(cached, RunState) and (cached.task_id, cached.run_id) == identity:
        return cached
    state = read_run_state(*identity)
    if state.state == "retired":
        try:
            agent._kanban_run_retired = state
        except Exception:
            pass
    return state


def admission_block(agent, tool_name: str) -> Optional[str]:
    """Refusal text when this worker may not dispatch ``tool_name``, else ``None``."""
    state = current_run_state(agent)
    if state is None or state.state == "live":
        return None
    if state.state == "retired":
        return (
            f"kanban_run_retired: run {state.run_id} of task {state.task_id} already ended "
            f"({state.status}). This worker is retired; `{tool_name}` was not executed and no "
            "further tools will run. End your turn now."
        )
    from tools.integrity_guard import READ_ONLY_TOOL_NAMES

    if tool_name in READ_ONLY_TOOL_NAMES or tool_name.startswith(_UNKNOWN_OWNERSHIP_ALLOWED_PREFIX):
        return None
    return (
        f"kanban_run_ownership_unknown: cannot verify that run {state.run_id} of task "
        f"{state.task_id} still owns this worker ({state.detail}); `{tool_name}` was refused. "
        "Retry after the board is readable, or call kanban_block with the diagnostic."
    )


def retirement_exit_message(state: RunState) -> str:
    """Assistant text that closes the turn once the run has ended."""
    return (
        f"Kanban run {state.run_id} for task {state.task_id} has ended ({state.status}); "
        "this worker is retired and stops here."
    )


__all__ = [
    "RETIRED_STATUSES", "TERMINAL_LIFECYCLE_TOOLS", "RunState", "admission_block",
    "current_run_state", "read_run_state", "retirement_exit_message", "worker_run_identity",
]
