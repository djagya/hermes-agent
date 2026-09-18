#!/usr/bin/env python3
"""Reject unguarded GitHub-hosted larger runners.

Larger runners (``*-NN-core``) are billed even on public repositories.
This fork keeps those labels only on jobs that cannot execute here
because the job ``if:`` requires ``github.repository == 'NousResearch/hermes-agent'``.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

LARGE_RUNNER = re.compile(
    r"(?:ubuntu|windows|macos)-latest-\d+(?:-arm)?-core"
)
NOUS_GUARD = "NousResearch/hermes-agent"
JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
IF_LINE = re.compile(r"^    if:\s*(?:>-|>)?\s*(.*)$")


def _job_blocks(text: str) -> list[tuple[str, str]]:
    """Return (job_id, job_text) pairs from a workflow file."""
    lines = text.splitlines()
    try:
        jobs_idx = next(
            i for i, line in enumerate(lines) if line == "jobs:" or line.startswith("jobs:")
        )
    except StopIteration:
        return []

    blocks: list[tuple[str, list[str]]] = []
    current: str | None = None
    buf: list[str] = []
    for line in lines[jobs_idx + 1 :]:
        match = JOB_KEY.match(line)
        if match:
            if current is not None:
                blocks.append((current, buf))
            current = match.group(1)
            buf = [line]
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        blocks.append((current, buf))
    return [(name, "\n".join(body)) for name, body in blocks]


def _job_if(job_text: str) -> str:
    lines = job_text.splitlines()
    collected: list[str] = []
    capturing = False
    for line in lines:
        if not capturing:
            match = IF_LINE.match(line)
            if not match:
                continue
            capturing = True
            remainder = match.group(1).strip()
            if remainder:
                collected.append(remainder)
            continue
        if line.startswith("      ") or (line.startswith("    ") and line.strip() == ""):
            collected.append(line.strip())
            continue
        break
    return " ".join(part for part in collected if part)


def find_unguarded_larger_runners(root: Path) -> list[str]:
    workflow_dir = root / ".github" / "workflows"
    if not workflow_dir.is_dir():
        raise ValueError(f"missing workflows directory: {workflow_dir}")

    failures: list[str] = []
    for path in sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        for job_id, job_text in _job_blocks(text):
            labels = sorted(set(LARGE_RUNNER.findall(job_text)))
            if not labels:
                continue
            if NOUS_GUARD in _job_if(job_text):
                continue
            failures.append(
                f"{rel}: job {job_id!r} uses {', '.join(labels)} "
                f"without if: github.repository == '{NOUS_GUARD}'"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject unguarded larger GitHub-hosted runners."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root to inspect (default: current directory)",
    )
    args = parser.parse_args(argv)

    try:
        failures = find_unguarded_larger_runners(args.root)
    except ValueError as exc:
        parser.error(str(exc))

    if not failures:
        print("No unguarded larger runners.")
        return 0

    print("::error::larger runners must be repository-guarded to NousResearch/hermes-agent")
    for item in failures:
        print(f"  {item}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
