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


def fill_expression(fills: list[dict[str, Any]], page_origin: str, nonce: str, target: Any, *, bind: str = "",
                    tokens: Optional[dict[str, str]] = None, refusal_tag: str = "") -> str:
    from agent.vault_login_classifier import build_fill_js, build_signup_fill_js

    if target is None:
        return build_fill_js(fills, expected_origin=page_origin, nonce=nonce, refusal_tag=refusal_tag)
    return build_signup_fill_js(fills, page_origin, nonce, target, bind=bind, tokens=tokens or {})


def outcome_tokens() -> dict[str, str]:
    """Fresh per-fill outcome tokens for the signup writer (see agent.vault_login_classifier.SIGNUP_OUTCOMES)."""
    from agent.vault_login_classifier import new_signup_outcome_tokens

    return new_signup_outcome_tokens()


_NO_SUBMIT = "Nothing was submitted. Do not submit the form and do not retry the fill; tell the user."
_FAILURES = {
    "signup_fill_partial": "The fill stopped part-way: the form changed or a write failed between fields, so up to "
                           "fields_possibly_written of the approved password fields may hold the password. "
                           + _NO_SUBMIT,
    "signup_form_changed": "The password was written into every approved field, but afterwards the form no longer "
                           "matched what was approved (a field, its type or role, the form or its action changed, or "
                           "another password or code field appeared), so the fill cannot be confirmed. " + _NO_SUBMIT,
    "signup_fields_not_retained": "The password was written, but after the page settled at least one approved field "
                                  "no longer held it (field_retention names which). " + _NO_SUBMIT,
    "signup_fill_uncertain": "The browser did not return a verifiable answer: one or more password fields may have "
                             "been written. " + _NO_SUBMIT,
}
_ROLES = ("new-password", "confirm-password")


def _failure(error_type: str, base: dict[str, Any], **extra: Any) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": _FAILURES[error_type],
                       "partially_filled": True, **extra, **base})


def _base(backend_name: str, kind: str, page_origin: str) -> dict[str, Any]:
    return {"backend": backend_name, "kind": kind, "origin": page_origin, "purpose": "signup"}


def uncertain(target: Any, backend_name: str, kind: str, page_origin: str) -> Optional[str]:
    """For a signup target, the result when the native write failed or never answered: a write may have
    happened, so never "Nothing was written". None for a login fill (its own handling)."""
    if target is None:
        return None
    return _failure("signup_fill_uncertain", _base(backend_name, kind, page_origin))


def finish(raw: Any, backend_name: str, kind: str, page_origin: str, target: Any,
           tokens: dict[str, str]) -> str:
    """Result of a signup write that the native transport ran: states only, never submitted, never a value.

    ``raw`` is the script's answer: one of this fill's outcome tokens, or anything else (garbled or forged by
    the page), which is uncertain. Success only when every approved field was written and still held the
    value after the page settled and the form was revalidated."""
    if isinstance(raw, str) and raw.startswith('"'):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    outcome = {token: name for name, token in tokens.items()}.get(raw) if isinstance(raw, str) else None
    base = _base(backend_name, kind, page_origin)
    fields = list(_ROLES[:len(target.controls)])
    if outcome == "none":
        return _refuse("signup_target_changed", "The approved signup target changed. Nothing was written.")
    if outcome == "origin":
        return _refuse("origin_changed", "The page navigated away from the approved origin before the fill. "
                                         "Nothing was written.")
    if outcome in ("partial:1", "partial:2") and int(outcome[-1]) <= len(fields):
        return _failure("signup_fill_partial", base, fields_possibly_written=int(outcome[-1]))
    if outcome == "changed":
        return _failure("signup_form_changed", base, fields=fields)
    bits = outcome[len("mask:"):] if outcome and outcome.startswith("mask:") else ""
    if len(bits) != len(fields):
        return _failure("signup_fill_uncertain", base)
    if bits == "1" * len(fields):
        nxt = ("Both password fields filled with the same saved password; nothing submitted. " if len(fields) == 2
               else "Password filled only; nothing submitted. ")
        return json.dumps({"success": True, "filled_fields": len(fields), "retention_verified": True,
                           "fields": fields, **base,
                           "next": nxt + "Review the account-creation form before submitting."})
    return _failure("signup_fields_not_retained", base, filled_fields=bits.count("1"),
                    field_retention={f: ("held" if b == "1" else "lost") for f, b in zip(fields, bits)})
