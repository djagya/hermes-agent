"""Provider credit and spending-limit exhaustion for cron runs.

A cron agent can finish its turn normally after a tool's provider refused service because the
account has no credits left. For example, ``x_search`` gets xAI's 403
``personal-team-blocked:spending-limit``, and the agent then writes a "could not check" report.
That run was recorded ``ok``. Runtime error text for the same condition was also misread: the 403
looked like a credential failure (``auth``), and OpenAI's ``insufficient_quota`` looked like a
transient ``rate_limit``.

This module holds one narrow matcher for that condition and uses it in three places:

* :func:`tool_budget_failure` finds the evidence in a finished run's tool results, so the run
  fails instead of recording ``ok``.
* the incident classifier types matching failures ``budget``.
* the chat notice says the account is out of credits, instead of telling the operator to sign in
  again.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

BUDGET_FAILURE_TYPE = "budget"

# Prefix of the run error recorded for a tool budget failure. The failure notice keys on it, the
# same way it keys on the script runner's "Script timed out" contract.
TOOL_BUDGET_ERROR_PREFIX = "Tool provider out of credits:"

# Lowercased substrings, each a provider's own code or wording for an exhausted balance or spend
# cap. Generic "limit"/"quota"/"credits" prose is deliberately absent: a rate limit or a usage
# window is transient, but these are not.
_BUDGET_MARKERS = (
    "personal-team-blocked:spending-limit",  # xAI error code (HTTP 403)
    "run out of credits",  # xAI: "You have run out of credits or need a Grok subscription."
    "used all available credits",  # xAI API-key teams: "...used all available credits or reached..."
    "insufficient_quota",  # OpenAI error code (HTTP 429)
    "exceeded your current quota",  # OpenAI: "You exceeded your current quota, please check..."
    "credit balance is too low",  # Anthropic: "Your credit balance is too low to access..."
    "insufficient_credits",  # OpenRouter and other aggregators (error code)
    "insufficient credits",  # OpenRouter 402 / Firecrawl: "Insufficient credits ..."
    "budget limit exceeded",  # OpenRouter org monthly cap: "Budget limit exceeded (monthly limit)"
)

# Tool results that already sit inside the untrusted-data wrapper (web_search, web_extract,
# browser_*, mcp_*) carry third-party content. A page quoting "run out of credits" is not
# evidence about our own account.
_UNTRUSTED_PREFIX = "<untrusted_tool_result"

_MAX_ERROR_CHARS = 400


def is_provider_budget_error(text: Any) -> bool:
    """True when *text* carries a provider's own credit or spending-limit exhaustion marker."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _BUDGET_MARKERS)


def _tool_payload(content: Any) -> Optional[dict]:
    """A tool result's leading JSON object, or None for text, wrapped or non-object content."""
    if not isinstance(content, str):
        return None
    text = content.lstrip()
    if not text.startswith("{"):
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def tool_budget_failure(messages: Iterable[Any]) -> Optional[str]:
    """Run error for a tool whose provider reported exhausted credits, or None.

    Only a tool's own structured error counts: a JSON result whose top-level ``error`` string
    carries a budget marker and whose ``success`` is not ``True``. A later successful result from
    the same tool clears its earlier failure, for example after the provider rotated to another
    credential. Untrusted-wrapped results are ignored.
    """
    failures: dict[str, str] = {}
    for message in messages or ():
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.lstrip().startswith(_UNTRUSTED_PREFIX):
            continue
        name = str(message.get("tool_name") or message.get("name") or "tool")
        payload = _tool_payload(content)
        error = payload.get("error") if payload is not None else None
        if (
            payload is not None and payload.get("success") is not True
            and isinstance(error, str) and is_provider_budget_error(error)
        ):
            provider = payload.get("provider")
            label = f"{name} ({provider})" if isinstance(provider, str) and provider.strip() else name
            detail = " ".join(error.split())[:_MAX_ERROR_CHARS]
            failures[name] = f"{TOOL_BUDGET_ERROR_PREFIX} {label}: {detail}"
        elif not (payload is not None and payload.get("error")):
            failures.pop(name, None)  # a later success from the same tool
    return next(iter(failures.values()), None)


def tool_budget_tool_label(error: str) -> str:
    """``x_search (xai)`` from a :func:`tool_budget_failure` error ("a tool" when unparseable)."""
    rest = str(error or "")[len(TOOL_BUDGET_ERROR_PREFIX):].strip()
    label = rest.split(":", 1)[0].strip()
    return label or "a tool"
