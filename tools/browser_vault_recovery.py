"""Stored recovery / backup codes into the page (D-0365; split from ``browser_vault_tool``, 11-18).

``browser_vault enter_code code_source=recovery field=<item field>`` types ONE stored code per human approval.
The origin is bound to the item before anything else; the approval names the item, the field and the site; the
code is read, chosen, marked spent (keyed by the item's canonical id) and written inside the backend's
fill_context, after the approval, into a field whose pixels are masked first. The code never enters a result.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any, Optional

from tools.browser_vault_context import fill_context, snapshot_refusal


def _refuse(error_type: str, error: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": error, **extra})


_NOT_CODES = ("The field {field!r} is not a recovery-code field (it is the login's password, username or "
              "one-time-password field, or its name does not say recovery / backup codes). Nothing was written.")


def _shown_field(field: str) -> str:
    """The agent-named field as the approval shows it: one line, printable, bounded."""
    return re.sub(r"\s+", " ", "".join(ch for ch in field if ch.isprintable())).strip()[:60]


def confirm_recovery_code(label: str, field: str, origin: str) -> str:
    from tools.browser_vault_signup import ask_consent

    return ask_consent(
        f"Enter one saved recovery code from the field '{_shown_field(field)}' of '{label}' on {origin}?",
        "The agent wants to type ONE of the recovery/backup codes saved in that field of this login into the code "
        "field of this exact site. The code never enters the conversation and is spent once used. Approve only if "
        "the field really holds recovery codes and you want Hermes to use one here.",
        surface="vault-recovery-code", title="Use a recovery code?")


def _choose_target(listed: list) -> tuple[Any, Optional[str]]:
    from agent.vault_login_classifier import classify_recovery_code_controls
    from tools import browser_vault_tool as vault

    targets = classify_recovery_code_controls(listed)
    if not targets:
        return None, json.dumps(vault._no_code_field_result(listed))
    top = max(t.score for t in targets)
    best = sorted((t for t in targets if t.score == top), key=lambda t: t.control.index)
    if len({t.control.form_index for t in best}) > 1:
        return None, _refuse("several_code_fields", "Several fields could take the recovery code, so none was "
                                                    "filled. Nothing was written.")
    return best[0].control, None


def _prepare(handle: str, task_id: str, field: str) -> tuple[Any, Any, str, Any, str, Optional[str]]:
    """``(backend, meta, origin, target, nonce, refusal)``: everything before the approval; nothing secret."""
    from agent.vault_recovery_codes import names_account_secret
    from tools import browser_vault_tool as vault

    none = (None, None, "", None, "")
    if not field.strip():
        return *none, _refuse("recovery_field_required", "code_source=recovery needs field: the name of the vault "
                                                         "item's field that holds the codes (e.g. \"recovery codes\"). "
                                                         "Nothing was written.")
    if names_account_secret(field):  # "password", "username", "one-time password": never read as codes
        return *none, _refuse("recovery_field_not_codes", _NOT_CODES.format(field=_shown_field(field)))
    backend, meta = vault._saved_login(handle)
    if meta is None and backend is not None and backend.needs_unlock and not backend.is_unlocked():
        return *none, _refuse("unlock_required", f"{backend.display_name} is locked; call browser_vault action=unlock.")
    if meta is None:
        return *none, _refuse("vault_item_not_found", "No saved login with this handle. Use browser_vault action=list.")
    if meta.kind != "login":
        return *none, _refuse("recovery_login_required", "Recovery codes are read from a saved login item. Nothing "
                                                         "was written.")
    origin = vault._bound_page_origin(task_id, meta, "otp")
    if isinstance(origin, dict):
        return *none, json.dumps(origin)
    nonce = secrets.token_hex(8)
    target, refusal = _choose_target(vault._page_controls(task_id, nonce))
    return backend, meta, origin, target, nonce, refusal


def _read_codes(backend: Any, handle: str, field: str, target: Any) -> tuple[list[str], Optional[str]]:
    """The item's codes that fit the target field, or a typed refusal. Inside fill_context."""
    from agent.vault_backends import BackendError, UnlockRequired
    from agent.vault_recovery_codes import RecoveryFieldNotCodes

    try:
        codes = backend.resolve_recovery_codes(handle, field)
    except UnlockRequired:
        return [], _refuse("unlock_required", f"{backend.display_name} locked again; call browser_vault action=unlock.")
    except RecoveryFieldNotCodes:
        return [], _refuse("recovery_field_not_codes", _NOT_CODES.format(field=_shown_field(field)),
                           next="Ask the user which field of the item holds the recovery codes.")
    except BackendError:
        raise  # typed manager failures carry advice and no secret
    except Exception:  # health: allow BLE001 -- untyped backend errors can carry secret bytes
        return [], _refuse("secret_resolution_failed", "Could not read the saved item. Nothing was written.")
    if not codes:
        return [], _refuse("recovery_codes_not_found", f"The saved login has no codes in a field named "
                                                       f"{_shown_field(field)!r}. Nothing was written.",
                           next="Ask the user which field holds them.")
    fit = [c for c in codes if target.max_length is None or len(c) <= target.max_length]
    if not fit:
        return [], _refuse("code_field_too_short", "The code field cannot hold a whole recovery code. Nothing was "
                                                   "written.")
    return fit, None


def _take(item_id: str, fit: list[str]) -> tuple[Optional[str], int, Optional[str]]:
    from agent.vault_recovery_codes import RecoveryLedgerUnreadable, take_unused

    try:
        code, left = take_unused(item_id, fit)
    except RecoveryLedgerUnreadable:
        return None, 0, _refuse("recovery_ledger_unreadable", "The record of recovery codes Hermes already used "
                                                              "cannot be read, so no code was chosen (it could "
                                                              "offer a spent one). Nothing was written.",
                                next="Tell the user; the file is vault/recovery_codes_used.json in the Hermes home.")
    if code is None:
        return None, 0, _refuse("recovery_codes_used_up", "Every saved recovery code was already used by Hermes. "
                                                          "Nothing was written.",
                                next="Tell the user to generate new recovery codes and update the vault item.")
    return code, left, None


def _written(result: dict, origin: str, left: int) -> str:
    from tools import browser_vault_outcome as outcome
    from tools import browser_vault_tool as vault

    if not result.get("success"):
        return _refuse(str(result.get("error_type") or "fill_failed"), "The browser did not confirm the code write; "
                       "the code counts as spent. Read the page before anything else.")
    parsed = vault._parse_json_result(result.get("result"))
    if isinstance(parsed, str):
        parsed = vault._parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return _refuse("origin_changed", "The page navigated before the code could be entered. Nothing was written.")
    filled = vault._filled_count(parsed, 1)
    if filled is None:
        return json.dumps(vault._FILL_UNVERIFIED)
    if not filled:
        reasons = outcome.fill_reasons(parsed, 1)
        return _refuse("code_not_written", "The code field did not take the code. Do not submit; read the page.",
                       reason=(reasons or ["unknown"])[0])
    return json.dumps({"success": True, "filled_fields": 1, "origin": origin, "source": "recovery_codes",
                       "codes_left": left,
                       "next": ("Submit the form. That code is now spent; Hermes will not type it again."
                                + (" Few codes are left: tell the user to generate new ones." if left <= 2 else ""))})


def enter_recovery_code(handle: str, task_id: str, field: str) -> str:
    """D-0365: one stored recovery code into the page's recovery/one-time-code field, one approval per code."""
    from agent.vault_login_classifier import build_fill_js
    from tools import browser_vault_tool as vault
    from tools.browser_vault_signup import not_given, outcome_of

    backend, meta, origin, target, nonce, refusal = _prepare(handle, task_id, field)
    if refusal is not None:
        return refusal
    outcome = outcome_of(confirm_recovery_code(meta.label, field, origin))
    if outcome != "accepted":
        return not_given("recovery_code_declined", "Using a recovery code", outcome)
    if vault._current_page_origin(task_id) != origin:
        return _refuse("origin_changed", "The page origin changed during approval. Nothing was written.")
    with fill_context(backend):
        refusal = snapshot_refusal(backend, handle, meta, origin)
        if refusal is not None:
            return json.dumps(refusal)
        fit, refusal = _read_codes(backend, handle, field, target)
        if refusal is not None:
            return refusal
        # The backend's canonical item id: "op:item" and "op:vault:item" name one item and share one record.
        code, left, refusal = _take(str(meta.id or handle), fit)
        if refusal is not None:
            return refusal
        refused = vault._register_redactions([code])
        if refused:
            return refused
        result = vault._eval_js_secret(task_id, build_fill_js(
            [{"index": target.index, "token": "one-time-code", "value": code, "mask": True}],
            expected_origin=origin, nonce=nonce))
        del code
    return _written(result, origin, left)
