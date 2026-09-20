"""Explicit-board operator recovery CLI, deliberately absent from worker tools."""

import json
import os
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_operator import (
    complete_triage_gate, require_operator_context, resolve_triage_blocker,
)
from hermes_cli.kanban_db_recovery import recovery_snapshot
from hermes_cli.kanban_output import _err


def operator_recovery_command(args):
    try:
        require_operator_context()
        board = getattr(args, "board", None)
        if not board or os.environ.get("HERMES_KANBAN_DB"):
            raise ValueError("operator recovery requires explicit --board and no HERMES_KANBAN_DB override")
        board = kb._normalize_board_slug(board)
        if not board:
            raise ValueError("--board requires a nonempty slug")
        request = {}
        if args.operation != "snapshot":
            if not args.request:
                raise ValueError("--request must name an operator-authored JSON request file")
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        elif args.request:
            raise ValueError("snapshot does not accept a mutation request")
        with kb.scoped_current_board(board):
            if not kb.kanban_db_path().is_file():
                raise ValueError("operator recovery requires an existing board database")
            with kbc.connect_closing() as conn:
                if args.operation == "snapshot":
                    print(json.dumps(recovery_snapshot(conn, args.task_id), indent=2))
                    return 0
                operation = {
                    "complete-gate": complete_triage_gate,
                    "resolve-blocker": resolve_triage_blocker,
                }[args.operation]
                if not operation(conn, args.task_id, **request):
                    return _err("operator recovery refused: stale/ineligible state or unmet acceptance; inspect a fresh snapshot")
                task = kb.get_task(conn, args.task_id)
                print(json.dumps({"task_id": args.task_id, "status": task.status if task else None}))
                return 0
    except (ValueError, TypeError, RuntimeError, PermissionError, OSError) as exc:
        return _err(f"kanban: {exc}")
