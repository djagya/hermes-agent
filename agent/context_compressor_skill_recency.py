"""Volume-based recency for just-loaded skill bodies (dlz, djagya/hermes-agent#99).

Upstream protects a skill_view body for a fixed window of trailing messages. The fork extends
that recency by context volume: walking back from the newest message, every message whose
running total still fits the look-back budget counts as recent, so a burst of short turns does
not demote a skill the model is still following. The look-back charges skill bodies and call
arguments, so a bulky run expires the extension. The ten-message floor and tail references keep
their semantics, and the Pass-4 pressure demotion still overrides.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

SKILL_PRUNE_RECENT_BUDGET_TOKENS = 8_000


def recent_skill_token_budget(compressor: Any, tail_fraction: float) -> int:
    """The look-back budget: the fixed cap, lowered to ``tail_fraction`` of a small model's window.

    Pruning can run without a resolved model window; that keeps the fixed cap rather than forcing
    a probe solely for this optional proportional cap.
    """
    budget = SKILL_PRUNE_RECENT_BUDGET_TOKENS
    context_length = getattr(compressor, "_resolved_context_length", None)
    if context_length is not None:
        window = compressor._effective_input_window(context_length, getattr(compressor, "max_tokens", None))
        budget = min(budget, int(window * tail_fraction))
    return budget


def volume_recent_start(messages: list[dict[str, Any]], recent_start: int, token_budget: int,
                        estimate: Callable[[dict[str, Any]], int]) -> int:
    """Lower ``recent_start`` to the oldest message whose newest-first running total still fits
    ``token_budget``; never raises it above the fixed-window floor the caller passed."""
    recent_tokens = 0
    for i in range(len(messages) - 1, -1, -1):
        recent_tokens += estimate(messages[i])
        if recent_tokens > token_budget:
            break
        recent_start = min(recent_start, i)
    return recent_start
