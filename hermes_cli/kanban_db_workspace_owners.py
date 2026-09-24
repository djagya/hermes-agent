"""Workspace writer ownership for Kanban handoff: may a successor mutate this workspace yet?

A committed handoff ends the predecessor's *run*; it does not end the predecessor's
*processes*. The worker may still be finalizing, and anything it launched (background
terminal jobs, servers, scripts) can keep writing. The dispatcher therefore asks this
module, before claiming a task whose workspace already exists, which live processes
still own that workspace.

Ownership is re-derived from live process attributes on every check instead of from
recorded PIDs, so:

- a recycled PID is never mistaken for the predecessor (it carries none of the markers);
- a dispatcher restart loses nothing (there is no in-memory lease to reconstruct);
- nothing is ever signalled — this module only observes.

Supported ownership model (processes of the dispatcher's own user):

1. ``HERMES_KANBAN_TASK`` equal to the task, or ``HERMES_KANBAN_WORKSPACE`` equal to
   the workspace, in the process environment (the dispatcher sets both for the worker;
   its terminal/background children inherit them);
2. current working directory inside the workspace;
3. an open regular file inside the workspace.

A descendant that scrubs its environment, leaves the workspace and holds no file open
there is outside this model. A process whose attributes cannot be read at all yields
``unknown`` and the workspace is held (fail closed), never freed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

WORKSPACE_HELD = "workspace_held"
WORKSPACE_OWNERSHIP_UNKNOWN = "workspace_ownership_unknown"

_MAX_REPORTED = 5


@dataclass
class WorkspaceOwnership:
    """``holders``: ``(pid, why)`` of live owning processes; ``unknown``: diagnostics for
    processes/scans whose ownership could not be decided. ``released`` iff both empty."""

    holders: list[tuple[int, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def released(self) -> bool:
        return not self.holders and not self.unknown

    def guard_reason(self) -> Optional[str]:
        if self.holders:
            return WORKSPACE_HELD
        if self.unknown:
            return WORKSPACE_OWNERSHIP_UNKNOWN
        return None

    def describe(self) -> str:
        parts = [f"pid {pid} ({why})" for pid, why in self.holders[:_MAX_REPORTED]]
        parts += self.unknown[:_MAX_REPORTED]
        return "; ".join(parts)


def _canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(path: Optional[str], root: str) -> bool:
    if not path:
        return False
    candidate = _canonical(path)
    return candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep)


def _self_and_ancestors() -> set[int]:
    """The checking process and its ancestors (e.g. the gateway hosting the dispatcher)
    are the observer, never the predecessor writer."""
    pids = {os.getpid()}
    try:
        import psutil

        pids.update(p.pid for p in psutil.Process().parents())
    except Exception:
        pids.add(os.getppid())
    return pids


def _owner_reason(proc: Any, task_id: str, root: str) -> tuple[Optional[str], bool]:
    """``(why, undecided)`` for one process. The environment is the primary marker, so
    an unreadable environment with no other evidence stays ``undecided`` (fail closed).

    One carve-out: ``cmdline`` stays readable for same-uid processes even where
    ``ptrace_scope`` (Yama) hides ``environ``/``cwd``/``open_files`` of non-relatives —
    on shared hosts (CI runners, multi-user boxes) pure fail-closed would hold the
    workspace forever on unrelated sibling noise. So when every ownership probe failed
    BUT the argv is readable and carries neither this task's worker marker
    (``work kanban task <id>`` — the dispatcher always injects it) nor a reference to
    the workspace path, the process is provably not this task's worker and the hold
    is released. A marker or path reference in argv is holder evidence."""
    import psutil

    env_unreadable = False
    try:
        env = proc.environ()
        env_task = env.get("HERMES_KANBAN_TASK")
        if env_task == task_id:
            return f"worker env for task {task_id}", False
        if env_task:
            # Another task's worker tree: not this task's predecessor, even if it shares
            # a ``dir:`` workspace (concurrent tasks on one dir were already admitted).
            return None, False
        if _within(env.get("HERMES_KANBAN_WORKSPACE"), root):
            return "worker env for this workspace", False
    except (psutil.AccessDenied, psutil.ZombieProcess):
        env_unreadable = True
    probe_failed = False
    try:
        if _within(proc.cwd(), root):
            return "cwd inside workspace", False
    except (psutil.AccessDenied, psutil.ZombieProcess):
        probe_failed = True
    try:
        for opened in proc.open_files():
            if _within(opened.path, root):
                return f"open file {opened.path}", False
    except (psutil.AccessDenied, psutil.ZombieProcess):
        probe_failed = True
    try:
        cmdline = proc.cmdline()
    except (psutil.AccessDenied, psutil.ZombieProcess, AttributeError):
        cmdline = None
    if cmdline:
        worker_marker = f"work kanban task {task_id}"
        if worker_marker in cmdline:
            return f"worker argv for task {task_id}", False
        if root and root in " ".join(cmdline):
            return "argv references workspace", False
        if env_unreadable:
            # Readable argv with no marker: not this task's worker. Releasing here is
            # the deliberate trade-off documented in the docstring — the alternative
            # (undecided) parks the successor forever on ptrace-protected hosts.
            return None, False
    return None, env_unreadable and probe_failed


def workspace_ownership(
    task_id: str, workspace: str, *, processes: Optional[Iterable[Any]] = None,
) -> WorkspaceOwnership:
    """Live owners of ``workspace`` for ``task_id``'s successor. ``processes`` is an
    injection seam (psutil-like objects); default is every live process."""
    result = WorkspaceOwnership()
    try:
        import psutil
    except ImportError:
        result.unknown.append("psutil unavailable: cannot verify workspace ownership")
        return result
    root = _canonical(workspace)
    observer = _self_and_ancestors()
    my_uid = os.getuid() if hasattr(os, "getuid") else None
    try:
        procs = list(processes) if processes is not None else list(psutil.process_iter())
    except Exception as exc:
        result.unknown.append(f"process scan failed: {type(exc).__name__}: {exc}")
        return result
    for proc in procs:
        try:
            if proc.pid in observer or proc.status() == psutil.STATUS_ZOMBIE:
                continue
            if my_uid is not None and proc.uids().real != my_uid:
                continue  # outside the supported model: other users' processes
            why, undecided = _owner_reason(proc, task_id, root)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue  # exited during the scan: not an owner
        except Exception as exc:
            result.unknown.append(f"pid {getattr(proc, 'pid', '?')}: {type(exc).__name__}")
            continue
        if why is not None:
            result.holders.append((proc.pid, why))
        elif undecided:
            result.unknown.append(f"pid {proc.pid}: ownership unreadable")
    return result


def successor_workspace_guard(task_id: str, workspace_path: Optional[str]) -> Optional[tuple[str, str]]:
    """``(reason, detail)`` when a successor must not yet be admitted to the task's
    recorded workspace, else ``None``. No recorded or no existing workspace = nothing
    to protect (a fresh workspace has no predecessor writers)."""
    if not workspace_path or not Path(workspace_path).is_dir():
        return None
    ownership = workspace_ownership(task_id, workspace_path)
    reason = ownership.guard_reason()
    if reason is None:
        return None
    return reason, ownership.describe()


def hold_for_predecessor_writers(conn: Any, task_id: str, *, dry_run: bool = False) -> Optional[str]:
    """Dispatcher gate run before claiming ``task_id``: the guard reason when the task's
    recorded workspace is still owned (or ownership is unknown), else ``None``.

    A held task stays in its lane untouched; the next tick re-checks, so release is
    picked up without any lease bookkeeping. A ``workspace_held`` event is appended
    only when the reason/detail changes, so a long hold is visible without flooding.
    """
    had_predecessor = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL LIMIT 1", (task_id,),
    ).fetchone()
    if had_predecessor is None:
        return None  # first run: no predecessor execution can own the workspace
    row = conn.execute("SELECT workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    guard = successor_workspace_guard(task_id, row["workspace_path"] if row else None)
    if guard is None:
        return None
    reason, detail = guard
    if not dry_run:
        from hermes_cli import kanban_db as kb

        payload = {"reason": reason, "detail": detail, "workspace": row["workspace_path"]}
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if last is None or last["kind"] != "workspace_held" or kb._json_or_null(payload) != last["payload"]:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, "workspace_held", payload)
    return reason


__all__ = [
    "WORKSPACE_HELD", "WORKSPACE_OWNERSHIP_UNKNOWN", "WorkspaceOwnership",
    "hold_for_predecessor_writers", "successor_workspace_guard", "workspace_ownership",
]
