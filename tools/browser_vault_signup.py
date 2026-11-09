"""``browser_vault`` fill with ``purpose=signup``: pick the new-password input (and its one explicitly
labelled confirmation input, if the form has one), ask the human, write.

Kept apart from the login fill so the default ``purpose=login`` path stays as it was: a signup fill
needs a fresh native approval naming the exact login item, origin and field scope BEFORE the password is
resolved, fills one explicit new-password input (or that input plus its confirmation, same value), and
never submits. A paired fill reports success only when both fields hold the value after the page settled.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# dlz: a consent that was not given has three causes (fault hunt 11-07 B17: a timed-out DataForSEO signup
# approval was reported as the user's refusal with "Do not retry"). Only a refusal is final.
CONSENT_OUTCOMES = ("refused", "timeout", "unavailable")
_NOT_GIVEN = {
    "refused": "The user refused.",
    "timeout": "Nobody answered the approval in time.",
    "unavailable": "The approval could not be shown to the user.",
}


def _refuse(error_type: str, error: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": error, **extra})


def ask_consent(message: str, description: str, *, surface: str, title: str) -> str:
    """One native consent: ``accepted``, ``refused``, ``timeout`` or ``unavailable``; one INFO line, no values."""
    from tools.approval_prompt import request_elicitation_consent

    detail: dict = {}
    try:
        verdict = request_elicitation_consent(message, description, surface=surface, title=title, outcome=detail)
    except Exception:  # health: allow BLE001 -- no approval surface means no consent: fail closed
        verdict, detail = "decline", {"reason": "unavailable"}
    reason = detail.get("reason")
    if verdict == "accept":
        reason = "accepted"
    elif reason not in CONSENT_OUTCOMES:  # a stub or a surface without a reason: infer from the verdict
        reason = "timeout" if verdict == "cancel" else "refused"
    logger.info("vault consent surface=%s outcome=%s", surface, reason)
    return reason


def outcome_of(answer: Any) -> str:
    """A consent helper's answer as an outcome; a plain bool (older callers, test stubs) is accepted/refused."""
    if answer is True or answer == "accepted":
        return "accepted"
    return answer if answer in CONSENT_OUTCOMES else "refused"


def not_given(error_type: str, what: str, outcome: str) -> str:
    """The stop result for a consent that was not given. ``error_type`` is unchanged (callers stop on it);
    ``consent_outcome`` says why, and only a refusal says "Do not retry"."""
    nxt = ("Do not retry." if outcome == "refused" else
           "This is not a refusal: ask the user to approve again (one new request), do not loop.")
    return _refuse(error_type, f"{what} was not approved. {_NOT_GIVEN[outcome]} Nothing was written. {nxt}",
                   consent_outcome=outcome)


def kind_refusal(purpose: str, kind: str) -> Optional[str]:
    """Signup creates an account with a saved login; any other item kind fails closed."""
    if purpose == "signup" and kind != "login":
        return _refuse("signup_login_required", "Signup requires a saved login item. Nothing was written.")
    return None


def confirm_signup_fill(handle: str, label: str, origin: str, paired: bool = False) -> str:
    """Fresh native human decision, never inherited from ordinary login permission; returns the outcome.
    ``paired``: the approval names both fields (new password and its confirmation)."""
    scope = ("into TWO fields of one registration form: the new-password field and its confirm-password "
             "field (the same password in both)" if paired else "into exactly one new-password field")
    return ask_consent(
        f"Create account on {origin} using saved login '{label}' (handle: {handle})?",
        f"Approve entering this saved password {scope} to create an account on this exact origin. This is not "
        "an ordinary login or password reset. The password never enters the conversation. Nothing will be "
        "submitted.",
        surface="vault-signup", title="Confirm signup password fill?")


def gate(purpose: str, raw_controls: list[Any], handle: str, label: str, page_origin: str,
         current_origin: Callable[[], str]) -> tuple[Any, Optional[str]]:
    """``(targets, refusal)``. Login: ``(None, None)``. Signup: the approved
    :class:`~agent.vault_login_classifier.SignupTargets`, or a refusal; the password has not been resolved
    either way."""
    if purpose != "signup":
        return None, None
    from agent.vault_login_classifier import LoginControl, select_signup_targets

    target = select_signup_targets([LoginControl.from_dict(raw) for raw in raw_controls if isinstance(raw, dict)])
    if target is None:
        return None, _refuse("signup_target_unsupported",
                             "Signup requires exactly one explicit new-password input on a registration form, "
                             "optionally with ONE explicitly labelled confirm-password input in the same form; "
                             "ambiguous password pairs, a third password, mixed login, OTP and recovery forms are "
                             "unsupported. Nothing was written.")
    outcome = outcome_of(confirm_signup_fill(handle, label, page_origin, paired=target.confirm is not None))
    if outcome != "accepted":
        return None, not_given("signup_declined", "Signup password fill", outcome)
    if current_origin() != page_origin:
        return None, _refuse("origin_changed", "The page origin changed during approval. Nothing was written.")
    return target, None


def password_fills(target: Any, classified: list[Any], password: str) -> list[dict[str, Any]]:
    """The signup target's new-password write (and its confirmation write, same value), or the ordinary
    login selection."""
    from agent.vault_login_classifier import select_password_fill

    if target is None or not password:
        return select_password_fill(classified, password)
    fills = [{"index": target.primary.index, "token": "new-password", "value": password}]
    if target.confirm is not None:
        fills.append({"index": target.confirm.index, "token": "confirm-password", "value": password})
    return fills


def fill_expression(fills: list[dict[str, Any]], page_origin: str, nonce: str, target: Any) -> str:
    from agent.vault_login_classifier import build_fill_js, build_signup_fill_js

    if target is None:
        return build_fill_js(fills, expected_origin=page_origin, nonce=nonce)
    return build_signup_fill_js(fills, page_origin, nonce, target)


_NO_SUBMIT = "Nothing was submitted. Do not submit the form and do not retry the fill; tell the user."
_PAIR_FAILURES = {
    "signup_fill_partial": "The new-password field may have been filled, but the confirm-password field was not "
                           "reliably filled: the form changed or a write failed after the first field "
                           "(fields_possibly_written says how many writes started). " + _NO_SUBMIT,
    "signup_fields_not_retained": "Both password fields were written, but after the page settled at least one no "
                                  "longer held the password (field_retention names which). " + _NO_SUBMIT,
    "signup_fill_uncertain": "The browser did not confirm the paired fill: one or both password fields may have "
                             "been written. " + _NO_SUBMIT,
}


def _pair_failure(error_type: str, base: dict[str, Any], **extra: Any) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": _PAIR_FAILURES[error_type],
                       "partially_filled": True, **extra, **base})


def uncertain(target: Any, backend_name: str, kind: str, page_origin: str) -> Optional[str]:
    """For a paired target, the result when the native write failed or its answer is unreadable: a write
    may have happened, so never "Nothing was written". None for a one-field target (unchanged)."""
    if target is None or target.confirm is None:
        return None
    return _pair_failure("signup_fill_uncertain", _base(backend_name, kind, page_origin))


def _base(backend_name: str, kind: str, page_origin: str) -> dict[str, Any]:
    return {"backend": backend_name, "kind": kind, "origin": page_origin, "purpose": "signup"}


def finish(parsed: Any, backend_name: str, kind: str, page_origin: str, target: Any = None) -> str:
    """Result of a signup write that the native transport ran: counts and states only, never submitted.

    One field: success when it was filled (unchanged). Pair: success only when BOTH fields were written
    and both still held the value after the page settled; otherwise a typed non-success."""
    if isinstance(parsed, dict) and parsed.get("refused") == "signup_target_changed":
        return _refuse("signup_target_changed", "The approved signup target changed. Nothing was written.")
    base = _base(backend_name, kind, page_origin)
    if target is not None and target.confirm is not None:
        if not isinstance(parsed, dict):
            return _pair_failure("signup_fill_uncertain", base)
        if parsed.get("partial") in ("signup_target_changed", "signup_write_failed"):
            attempted = parsed.get("attempted")
            return _pair_failure("signup_fill_partial", base, reason=parsed["partial"],
                                 fields_possibly_written=attempted if attempted in (1, 2) else 2)
        retained = parsed.get("retained")
        if parsed.get("filled") != 2 or not (isinstance(retained, list) and len(retained) == 2):
            return _pair_failure("signup_fill_uncertain", base)
        if retained != [True, True]:
            states = {token: ("held" if held is True else "lost")
                      for token, held in zip(("new-password", "confirm-password"), retained)}
            return _pair_failure("signup_fields_not_retained", base, filled_fields=sum(h is True for h in retained),
                                 field_retention=states)
        return json.dumps({"success": True, "filled_fields": 2, "retention_verified": True,
                           "fields": ["new-password", "confirm-password"], **base,
                           "next": "Both password fields filled with the same saved password; nothing submitted. "
                                   "Review the account-creation form before submitting."})
    filled = parsed.get("filled", 0) if isinstance(parsed, dict) else 0
    return json.dumps({"success": bool(filled), "filled_fields": int(filled), **base,
                       "next": "Password filled only; nothing submitted. Review the account-creation form before "
                               "submitting."})
