"""Phase-aware goal-judge objectives for Kanban lifecycle handoffs.

A Kanban task may have criteria that can only be produced after implementation
enters independent review. The transition into review must therefore be judged
against review readiness, while final completion remains judged against the
whole task.
"""

from __future__ import annotations

from typing import Literal


HandoffPhase = Literal["completion", "review"]

_REVIEW_HANDOFF_PREFIX = (
    "Evaluate readiness for the review handoff, not final task completion. "
    "Return DONE when the implementation-phase work is complete enough for an "
    "independent reviewer to begin and the handoff contains concrete verification "
    "evidence. All implementation requirements and constraints remain binding. "
    "Do not require the independent review verdict, reviewer-authored evidence, "
    "final approval, or downstream activation that can only happen after this "
    "handoff.\n\nOriginal task:\n"
)


def build_goal_judge_objective(
    *,
    title: str,
    body: str | None,
    phase: HandoffPhase,
) -> str:
    """Build the objective judged at one Kanban lifecycle transition."""
    task_goal = f"{title}\n\n{body or ''}".strip()
    if phase == "completion":
        return task_goal
    if phase == "review":
        return f"{_REVIEW_HANDOFF_PREFIX}{task_goal}"
    raise ValueError(f"unsupported Kanban handoff phase: {phase}")
