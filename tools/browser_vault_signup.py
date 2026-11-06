"""``browser_vault`` fill with ``purpose=signup``: pick the one new-password input, ask the human, write.

Kept apart from the login fill so the default ``purpose=login`` path stays as it was: a signup fill
needs a fresh native approval naming the exact login item and origin BEFORE the password is
resolved, fills exactly one explicit new-password input, and never submits.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional


def _refuse(error_type: str, error: str) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": error})


def kind_refusal(purpose: str, kind: str) -> Optional[str]:
    """Signup creates an account with a saved login; any other item kind fails closed."""
    if purpose == "signup" and kind != "login":
        return _refuse("signup_login_required", "Signup requires a saved login item. Nothing was written.")
    return None


def confirm_signup_fill(handle: str, label: str, origin: str) -> bool:
    """Fresh native human decision, never inherited from ordinary login permission."""
    from tools.approval_prompt import request_elicitation_consent

    try:
        return request_elicitation_consent(
            f"Create account on {origin} using saved login '{label}' (handle: {handle})?",
            "Approve entering this saved password into exactly one new-password field to create an account "
            "on this exact origin. This is not an ordinary login or password reset. The password never enters "
            "the conversation. Nothing will be submitted; confirmation-password fields are unsupported.",
            surface="vault-signup", title="Confirm signup password fill?") == "accept"
    except Exception:  # health: allow BLE001 -- no approval surface means no consent: fail closed, nothing resolved
        return False


def gate(purpose: str, raw_controls: list[Any], handle: str, label: str, page_origin: str,
         current_origin: Callable[[], str]) -> tuple[Any, Optional[str]]:
    """``(target, refusal)``. Login: ``(None, None)``. Signup: the approved target, or a refusal; the
    password has not been resolved either way."""
    if purpose != "signup":
        return None, None
    from agent.vault_login_classifier import LoginControl, select_signup_control

    target = select_signup_control([LoginControl.from_dict(raw) for raw in raw_controls if isinstance(raw, dict)])
    if target is None:
        return None, _refuse("signup_target_unsupported",
                             "Signup requires exactly one explicit new-password input on a registration form; "
                             "confirmation, mixed login, OTP and recovery forms are unsupported. Nothing was written.")
    if not confirm_signup_fill(handle, label, page_origin):
        return None, _refuse("signup_declined",
                             "Signup password fill was not approved. Nothing was written. Do not retry.")
    if current_origin() != page_origin:
        return None, _refuse("origin_changed", "The page origin changed during approval. Nothing was written.")
    return target, None


def password_fills(target: Any, classified: list[Any], password: str) -> list[dict[str, Any]]:
    """The signup target's one new-password write, or the ordinary login selection."""
    from agent.vault_login_classifier import select_password_fill

    if target is None or not password:
        return select_password_fill(classified, password)
    return [{"index": target.control.index, "token": "new-password", "value": password}]


def fill_expression(fills: list[dict[str, Any]], page_origin: str, nonce: str, target: Any) -> str:
    from agent.vault_login_classifier import build_fill_js, build_signup_fill_js

    if target is None:
        return build_fill_js(fills, expected_origin=page_origin, nonce=nonce)
    return build_signup_fill_js(fills, page_origin, nonce, target.control)


def finish(parsed: Any, backend_name: str, kind: str, page_origin: str) -> str:
    """Result of a signup write that the native transport ran: counts only, never submitted."""
    if isinstance(parsed, dict) and parsed.get("refused") == "signup_target_changed":
        return _refuse("signup_target_changed", "The approved signup target changed. Nothing was written.")
    filled = parsed.get("filled", 0) if isinstance(parsed, dict) else 0
    return json.dumps({"success": bool(filled), "filled_fields": int(filled), "backend": backend_name,
                       "kind": kind, "origin": page_origin, "purpose": "signup",
                       "next": "Password filled only; nothing submitted. Review the account-creation form before "
                               "submitting."})
