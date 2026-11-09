"""Startup auto-resume of turns cut off by a restart/shutdown drain (moved out of ``gateway/run.py``).

``gateway.resume_interrupted: continue`` (fork default) resumes the interrupted request, quoted from the
transcript; ``ask`` keeps upstream's "report the restore and ask what next" on interactive platforms.
Non-interactive event platforms (webhook, API server) always continue: nobody is there to answer (#57056).
The resume event is internal and blank, so ``_prefix_inbound_sender_context`` must not attribute it: a
"[Name] " prefix made it read as a NEW, empty user message (fork 2026-10-21/22 deploys).
"""

from __future__ import annotations

from typing import Any, Optional


def resume_note_interactive(runner: Any, source: Any) -> bool:
    """True = the auto-resume asks what next (``resume_interrupted: ask`` on an interactive platform)."""
    if getattr(getattr(runner, "config", None), "resume_interrupted", "continue") != "ask":
        return False
    return bool(getattr(runner._delivery_adapter_for(source), "interactive_resume", True))


# Cap on the original request quoted into the resume note (the full row stays in the history).
_RESUME_REQUEST_QUOTE_MAX_CHARS = 4000
_RESUME_NOTE_PREFIX = "[System note: The previous turn was interrupted by "


def _message_text(content: Any) -> str:
    """Plain text of a transcript ``content`` (str, or an OpenAI-style parts list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") == "text").strip()
    return ""


def interrupted_request_text(history: Any) -> str:
    """Text of the user request the interrupted turn was working on (latest real user row), or "".

    Skips earlier resume notes (a turn interrupted twice) and blank rows, so the resumed turn quotes what
    the user actually asked rather than a scaffold or an empty attribution prefix."""
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _message_text(msg.get("content")).strip()
        if not text or text.startswith(_RESUME_NOTE_PREFIX):
            continue
        if len(text) > _RESUME_REQUEST_QUOTE_MAX_CHARS:
            text = text[:_RESUME_REQUEST_QUOTE_MAX_CHARS] + " […]"
        return text
    return ""


def build_resume_recovery_note(
    reason: Optional[str], message: str = "", *, interactive: bool = True, original_request: str = "") -> str:
    """Build the resume-pending recovery system note for an interrupted turn (empty ``message`` = auto-resume).

    ``interactive=True`` (``gateway.resume_interrupted: ask`` on a platform with a human): report the restore
    and ask what next. ``interactive=False``: CONTINUE the interrupted work — the fork default for every
    platform, and the only option on event platforms (webhook, API server) where nobody can answer and a
    "restored" acknowledgement would silently abandon the task (#57056). ``original_request`` is the
    interrupted user request, quoted so the model resumes THAT task (a restart mid-turn otherwise left an
    empty user turn and the model asked what the user wanted).
    """
    reason_phrase = (
        "a gateway restart" if reason == "restart_timeout"
        else "a gateway shutdown" if reason == "shutdown_timeout" else "a gateway interruption")
    request_block = ""
    if message:
        resume_guidance = (
            "Address the user's NEW message below FIRST and focus on what the user is asking now.")
        tail_guidance = (
            "Do NOT re-execute old tool calls — skip any unfinished work from the conversation history."
        )
    elif interactive:
        resume_guidance = (
            "Report to the user that the session was restored "
            "successfully and ask what they would like to do next.")
        tail_guidance = (
            "Do NOT re-execute old tool calls — skip any unfinished work from the conversation history."
        )
    else:
        resume_guidance = (
            "This is an automatic resume, not a new user message: do NOT emit a 'session restored' "
            "acknowledgement or ask what to do next. Review the conversation history and "
            "CONTINUE the interrupted task to completion.")
        tail_guidance = (
            "Do NOT re-run tool calls whose results already "
            "appear in the history — resume from the first step that has no recorded result.")
        if original_request:
            request_block = f"\n\nThe interrupted request was:\n<interrupted_request>\n{original_request}\n</interrupted_request>"
    return (
        f"{_RESUME_NOTE_PREFIX}"
        f"{reason_phrase}; the gateway is now back online. "
        f"Any restart/shutdown command in the history has already "
        f"run — do NOT re-execute or verify it. {resume_guidance} {tail_guidance}]"
        + request_block
        + (f"\n\n{message}" if message else ""))


def _prepare_resume_pending_message(
    reason: Optional[str], message: Optional[str], *, interactive: bool = True,
    original_request: str = "") -> tuple[str, str]:
    """Return the recovery message and the user text to persist.

    Empty original: persist the note (a "" user row trips the pre-call sanitizer). Real text: persist clean.

    Resume turns replace the startup event's text with a recovery note before entering the agent. When the
    original message is empty (the synthesized auto-resume turn), persist the note too — persisting the
    empty string left a blank user row in state.db that the pre-call sanitizer re-healed on every later call
    forever (#86580). When the user sent REAL text while the resume was pending, keep persisting their clean
    words: the transcript stays scaffold-free (the model still receives the wrapped note), and a non-empty
    row never trips the sanitizer.
    """
    recovery_message = build_resume_recovery_note(
        reason, message or "", interactive=interactive, original_request=original_request)
    persist_message = message if isinstance(message, str) and message.strip() else recovery_message
    return recovery_message, persist_message
