"""Reading a vault write's answer (split from ``browser_vault_tool``, 11-18): the login writer's per-field
codes and settle read-back, the ``form_in_frame`` refusal for frames the inspection cannot enter, and the one
INFO record per vault action. Counts and fixed codes only: nothing here sees a secret value."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

# The records keep the facade's logger name (operators grep "tools.browser_vault_tool").
logger = logging.getLogger("tools.browser_vault_tool")

LOGIN_REASONS = {
    "password_field_ambiguous": ("Several password fields fit equally well (in different frames or origins), so "
                                 "none was filled. Nothing was written."),
    "password_outside_open_dialog": ("A dialog is open on the page and holds no password field; the password "
                                     "fields behind it were not filled. Nothing was written."),
}


def fill_reasons(parsed: Any, most: int) -> Optional[list[str]]:
    """The writer's per-field codes, or None when absent or not one fixed code per fill (never echoed)."""
    from agent.vault_login_classifier import FILL_REASONS

    reasons = parsed.get("reasons") if isinstance(parsed, dict) else None
    if not isinstance(reasons, list) or len(reasons) != most or not all(r in FILL_REASONS for r in reasons):
        return None
    return list(reasons)


def frames_refusal(task_id: str, meta: Any) -> Optional[dict[str, Any]]:
    """``form_in_frame`` when the page embeds frames the inspection could not enter (another origin): the form
    may be inside one. Same-origin frames are already inspected and filled like the page itself. Names only the
    frame origins that are saved on the item (they can be opened directly); counts the rest."""
    from agent.vault_login_classifier import FRAME_PROBE_JS
    from tools import browser_vault_tool as vault
    from tools.browser_vault_context import saved_origins

    res = vault._eval_js(task_id, FRAME_PROBE_JS)
    origins = vault._parse_json_result(res.get("result")) if res.get("success") else None
    if isinstance(origins, str):
        origins = vault._parse_json_result(origins)
    if not isinstance(origins, list):
        return None
    frames = [o for o in origins if isinstance(o, str) and o.startswith(("https://", "http://"))][:20]
    if not frames:
        return None
    saved = [o for o in frames if meta is not None and o in saved_origins(meta)]
    out: dict[str, Any] = {"success": False, "error_type": "form_in_frame", "frames_not_inspected": len(frames),
                           "error": ("No fillable field on the page itself, and it embeds frames from another origin "
                                     "that Hermes cannot reach from the page; the form may be inside one. Nothing "
                                     "was written.")}
    if saved:
        out["saved_frame_origins"] = saved
        out["next"] = ("A frame is on an origin saved on this item: open that frame's address in the tab with "
                       "browser_exec, then call fill again.")
    else:
        out["next"] = "Open the login form's own page (the frame's address) if you can find it, else ask the user."
    return out


def _not_written(meta: Any, base: dict, filled: int, reasons: Optional[list[str]], password_missed: bool,
                 pw_at: Optional[int]) -> str:
    # dlz (11-18): never "Submit" after a write that did not land; say why, per field (fixed codes).
    reason = (reasons[pw_at] if password_missed and reasons is not None and pw_at is not None else
              next((r for r in reasons or [] if r != "written"), "unknown"))
    return json.dumps({"success": False, "error_type": "login_fill_not_written" if meta.kind == "login"
                       else "fill_not_written", "filled_fields": filled, "reason": reason,
                       **({"field_reasons": reasons} if reasons else {}), **base,
                       "error": ("The password did not go into the field. Do not submit." if password_missed
                                 and filled else "No field took the value. Nothing was written; do not submit."),
                       "next": "Read the form (it may have re-rendered or moved to another step), then fill again "
                               "once; if it fails the same way, tell the user."})


def on_bank_slot(task_id: str) -> bool:
    """The tab's browser slot is a sensitive* (bank-like) one, or unknown: fail closed, nothing in the vault marks
    a bank, so an unknown slot is treated like one."""
    from tools.browser_use_cli import vault_route

    slot = str(vault_route(task_id).get("slot_id") or "")
    return not slot or slot.startswith("sensitive")


def login_write_result(parsed: Any, fills: list[dict[str, Any]], settle: int, backend: Any, meta: Any,
                       page_origin: str, bank: bool = False) -> str:
    """The tool result for a login/address/card-field write the page answered (not signup/reset)."""
    from tools import browser_vault_tool as vault

    filled = vault._filled_count(parsed, len(fills))
    if filled is None:
        return json.dumps(vault._FILL_UNVERIFIED)
    reasons = fill_reasons(parsed, len(fills))
    from agent.vault_captcha import challenge_fields

    base = {"backend": backend.name, "kind": meta.kind, "origin": page_origin, **challenge_fields(parsed, bank)}
    pw_at = next((i for i, f in enumerate(fills) if f["token"] == "current-password"), None)
    password_missed = reasons is not None and pw_at is not None and reasons[pw_at] != "written"
    if filled == 0 or password_missed:
        return _not_written(meta, base, filled, reasons, password_missed, pw_at)
    navigated = settle > 0 and isinstance(parsed, dict) and parsed.get("navigated") is True
    retained = parsed.get("retained") if isinstance(parsed, dict) else None
    retention_known = settle > 0 and not navigated and type(retained) is int and 0 <= retained <= filled
    if retention_known and retained < filled:
        return json.dumps({"success": False, "error_type": "password_not_retained", "filled_fields": filled,
                           "retained_fields": retained, "partially_filled": True, **base,
                           "error": ("The password was written, but the page cleared it again after it settled. "
                                     "Nothing was submitted. Do not submit; read the form and fill again once, or "
                                     "tell the user.")})
    out: dict[str, Any] = {"success": True, "filled_fields": filled, **base}
    if navigated:  # the site submitted on its own during the settle: the write landed, retention is moot
        out["retention_verified"] = None
        out["navigated_after_write"] = True
    elif settle > 0:
        out["retention_verified"] = retention_known
    if reasons and any(r != "written" for r in reasons):
        out["field_reasons"] = reasons
    if meta.kind == "login":
        out["next"] = (("The page moved on by itself after the fill: read it. " if navigated else "Submit. ")
                       + "If the site then asks for a verification code, call browser_vault action=enter_code with "
                       "this handle" + (" (a code will be generated automatically)." if meta.has_otp else "."))
    else:
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return json.dumps(out)


LOGGED_ACTIONS = ("fill", "enter_code", "save_login", "letter_login", "reset_password", "enroll_totp")


def log_outcome(args: dict[str, Any], result: dict[str, Any]) -> None:
    """dlz (11-18): one INFO line per fill / enter_code (signup and reset are fill purposes): counts and fixed
    codes only, never a value, a page text, an origin or a handle."""
    action = str(args.get("action") or "")
    if action not in LOGGED_ACTIONS:
        return

    def code(key: str) -> str:
        value = result.get(key)
        return value if isinstance(value, str) and value.replace("_", "").replace("-", "").isalnum() and len(value) <= 48 else "-"

    def count(key: str) -> str:
        value = result.get(key)
        return str(value) if type(value) is int else "-"

    purpose = str(args.get("purpose") or "login") if action == "fill" else str(args.get("code_source") or "-")
    logger.info("browser_vault %s purpose=%s success=%s error_type=%s reason=%s kind=%s filled=%s retained=%s",
                action, purpose if purpose in ("login", "signup", "reset", "recovery", "otp", "-") else "other",
                result.get("success") is True, code("error_type"), code("reason"), code("kind"),
                count("filled_fields"), count("retained_fields"))
