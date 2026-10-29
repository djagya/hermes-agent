#!/usr/bin/env python3
"""Vault-backed model-blind browser autofill tools.

One model-facing tool, ``browser_vault``, deferred behind tool_search (only its catalog line is
paid per request) and available wherever a browser is. Its actions:

- ``list``  → handles + metadata (for logins this includes the
  identifier — it is NOT a secret; the agent types it itself). Passwords are
  never returned.
- ``fill``  → server-side fill of the CURRENT page from a vault
  handle: the password field for logins, card fields for payment items (after
  the user confirms), address fields for address items. The secret is
  resolved locally, the page origin must EXACTLY match the item's bound
  origin (pre-checked AND re-asserted synchronously inside the fill script),
  the field is chosen by the ported login-control classifier, injection runs
  exclusively over the supervisor CDP WebSocket (never argv), and the tool
  result reports only ``{filled_fields, kind, origin, success}`` — the
  password never appears in tool results, logs, or the session DB, and its
  exact bytes are registered with the browser-result redaction boundary so
  no later browser tool call can echo them back to the model.

Ported design from Merit-Systems/OpenInstinct (MIT): opaque-handle vault
autofill (kernel-login-autofill.ts / fill_from_vault.ts).
"""

from __future__ import annotations

import json
import secrets
import logging
from typing import Any, Dict, Optional
from agent.vault_payment_policy import PaymentPolicyError, effective_frame_origins

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_vault_available() -> bool:
    """Schema-gate: the vault rides with the browser. An empty vault still needs save_login so the
    agent can offer to remember a login the first time it meets a form; hiding the tool until an item
    exists meant nobody ever discovered the feature. Deferral keeps that presence nearly free."""
    from tools.browser_tool_install import check_browser_requirements
    from tools.browser_use_cli import is_browser_use_cli_mode
    # check_browser_requirements() is False by design in Browser Use mode (browser_exec replaces the
    # built-in surface); the vault serves both stacks.
    return bool(is_browser_use_cli_mode() or check_browser_requirements())


# ---------------------------------------------------------------------------
# JS evaluation plumbing (server-side; results never carry secret values)
# ---------------------------------------------------------------------------

class _PageUnavailable(Exception):
    """Why the vault cannot reach the page the agent drives; surfaced as a typed tool result."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type

    def result(self) -> dict[str, Any]:
        return {"success": False, "error_type": self.error_type, "error": str(self)}


def _is_managed() -> bool:
    try:
        from tools.browser_control_route import is_managed
    except ImportError:
        return False
    return is_managed()


def _managed_supervisor(task_id: str, *, admit: bool = False):
    """Managed mode: the supervisor on the browser browser_exec last drove for this task.

    Never acquires: a fresh lease is a different browser without the page, and the legacy session
    path acquires without the exec's identity. A dropped socket is re-attached to the same lease after
    control confirms the agent still holds it; ``admit`` asks control even when the socket looks live
    (start of every vault page operation, and again right before a secret is written)."""
    from tools.browser_control_route import _lease_alive, control_url
    from tools.browser_supervisor import SUPERVISOR_REGISTRY, _redact_cdp_error_text
    from tools.browser_use_cli import vault_route

    route = vault_route(task_id)
    if not route:
        raise _PageUnavailable("no_browser_session", "This task has no managed browser page. Open the login page "
                               "with browser_exec (same task), then retry.")
    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    active = supervisor is not None and supervisor.snapshot().active
    if active and not admit:
        return supervisor
    alive = _lease_alive(control_url(), route.get("lease", ""))
    if alive is None:
        raise _PageUnavailable("lease_unavailable", "Managed browser control did not answer the lease check. Retry.")
    if not alive:
        raise _PageUnavailable("lease_unavailable", "The managed browser lease ended (released, expired, recycled, "
                               "or a human took control). Re-open the page with browser_exec, then retry.")
    if active:
        return supervisor
    from tools.browser_tool_cdp import _get_dialog_policy_config, _resolve_cdp_override
    policy, timeout_s = _get_dialog_policy_config()
    try:
        return SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=_resolve_cdp_override(route["cdp"]),
                                                dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:  # health: allow BLE001 -- re-raised as _PageUnavailable; a traceback would leak the CDP endpoint
        cause = _redact_cdp_error_text(exc)[:200]
        logger.warning("vault: supervisor attach to the managed browser failed: %s", cause)
        raise _PageUnavailable("supervisor_unavailable",
                               f"Could not open the vault's CDP connection to the managed browser: {cause}") from None


def _eval_js(task_id: str, expression: str) -> dict[str, Any]:
    """Evaluate NON-SECRET JS on the current page (inspection, origin reads).

    Prefers the supervisor's persistent CDP WebSocket, falls back to the
    agent-browser CLI ``eval`` command (never in managed mode, where that
    session is a different browser). Never use this for expressions that
    embed secret values — the fallback places the expression in subprocess
    argv. Use :func:`_eval_js_secret` for secret-bearing expressions.
    """
    if _is_managed():
        sup = _managed_supervisor(task_id).evaluate_runtime(expression)
        if sup.get("ok"):
            return {"success": True, "result": sup.get("result")}
        return {"success": False, "error": str(sup.get("error") or "eval failed")}
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY

        supervisor = SUPERVISOR_REGISTRY.get(task_id)
        if supervisor is not None:
            sup = supervisor.evaluate_runtime(expression)
            if sup.get("ok"):
                return {"success": True, "result": sup.get("result")}
            err = str(sup.get("error") or "")
            if "supervisor" not in err.lower():
                return {"success": False, "error": err}
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("vault fill: supervisor eval unavailable (%s)", exc)

    from tools.browser_tool import _last_session_key
    from tools.browser_tool_session import _run_browser_command

    effective = _last_session_key(task_id)
    result = _run_browser_command(effective, "eval", [expression])
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "eval failed")}
    return {"success": True, "result": result.get("data", {}).get("result")}


def _ensure_supervisor(task_id: str):
    """The supervisor for ``task_id``, attaching one on demand for a LOCAL built-in browser session.

    Cloud/CDP-override sessions and browser_exec attach their supervisor when the session is created;
    a local agent-browser ``--session`` has no ``cdp_url`` of its own, so nothing did. Ask the daemon
    for the packaged Chromium's endpoint (``get cdp-url``: same daemon, same reaper) and attach.
    Returns None when no endpoint is reachable; the fill then refuses rather than touching argv.
    Managed mode raises :class:`_PageUnavailable` instead (see :func:`_managed_supervisor`)."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    if _is_managed():
        return _managed_supervisor(task_id)
    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is not None:
        return supervisor
    from tools.browser_tool import _last_session_key
    from tools.browser_tool_cdp import _get_dialog_policy_config, _resolve_cdp_override
    from tools.browser_tool_session import _run_browser_command

    res = _run_browser_command(_last_session_key(task_id), "get", ["cdp-url"])
    cdp_url = str(((res or {}).get("data") or {}).get("cdpUrl") or "") if (res or {}).get("success") else ""
    if not cdp_url:
        return None
    policy, timeout_s = _get_dialog_policy_config()
    try:
        return SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=_resolve_cdp_override(cdp_url),
                                                dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        logger.debug("vault fill: supervisor attach to local session failed (%s)", exc)
        return None


def _eval_js_secret(task_id: str, expression: str) -> dict[str, Any]:
    """Evaluate a SECRET-BEARING JS expression. Supervisor CDP-WS only.

    Fails closed: there is deliberately NO fallback to the agent-browser CLI
    ``eval`` path, because that places the expression — and therefore the
    credential bytes — in subprocess argv, visible to any process listing.
    When no supervisor session is available the caller gets a typed refusal
    (``error_type='supervisor_required'``) and nothing is written.
    """
    try:
        supervisor = _managed_supervisor(task_id, admit=True) if _is_managed() else _ensure_supervisor(task_id)
    except _PageUnavailable as exc:
        return exc.result()
    except Exception as exc:
        logger.debug("vault fill: supervisor unavailable (%s)", exc)
        supervisor = None

    if supervisor is None:
        return {
            "success": False,
            "error_type": "supervisor_required",
            "error": (
                "Vault fill requires the supervised browser session (direct "
                "CDP WebSocket). The fallback eval path would place the "
                "credential in subprocess argv, so it is never used for "
                "secrets. Start the browser through the Hermes-managed "
                "session and retry."
            ),
        }

    # Re-admit at the WRITE. The handler-level fence (_fenced_page_op) admitted before a possibly
    # human-length prompt (enter_code waits for the user's code); a takeover during that wait must
    # refuse here, before the credential lands in a page the human is now typing into. The outer
    # epoch check only discards the result, and a fill is a side effect, not a result.
    if _bot_desktop_browser_session(task_id):
        from tools.bot_desktop import lease as _bd_lease
        try:
            _bd_lease.assert_agent_may_act()
        except _bd_lease.HumanHasControl as exc:
            return {"success": False, "error_type": "human_has_control", "error": str(exc)}

    sup = supervisor.evaluate_runtime(expression)
    if sup.get("ok"):
        return {"success": True, "result": sup.get("result")}
    return {
        "success": False,
        "error_type": "supervisor_required"
        if "supervisor" in str(sup.get("error") or "").lower()
        else "eval_failed",
        "error": str(sup.get("error") or "eval failed"),
    }


def _parse_json_result(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _current_page_origin(task_id: str) -> Optional[str]:
    res = _eval_js(task_id, "window.location.href")
    if not res.get("success"):
        return None
    href = str(res.get("result") or "").strip().strip('"').strip("'")
    if not href or href == "about:blank":
        return None
    try:
        from agent.vault_store import normalize_origin

        return normalize_origin(href)
    except Exception:
        return None


# Per kind: a JS probe that is truthy on a tab holding the form this kind fills.
_TAB_PROBES = {
    "login": "!!document.querySelector('input[type=password]')",
    "payment": "!!document.querySelector('input[autocomplete^=cc-], [name*=card i], [placeholder*=card i], [name*=cvc i], [name*=cvv i]')",
    "address": "!!document.querySelector('input[autocomplete^=address-], [autocomplete=postal-code], [name*=address i], [name*=zip i], [name*=postal i]')",
}


def _focus_bound_origin(task_id: str, origin: str, kind: str) -> Optional[str]:
    """Point the supervisor's page session at the open tab on ``origin`` that holds a ``kind`` form
    (browser_exec sessions open their own tabs, so the tab the supervisor attached to first is rarely the
    login page). Returns the origin when a tab was focused, else None (caller falls back to the current page)."""
    try:
        supervisor = _ensure_supervisor(task_id)
    except _PageUnavailable:
        raise
    except Exception:
        supervisor = None
    if supervisor is None:
        return None
    focused = supervisor.focus_page(origin, accept=_TAB_PROBES.get(kind))
    if not focused.get("ok"):
        # The probe reads only the top document, so a field in a shadow root or an iframe is invisible to it.
        # The supervisor's previous page is usually the exec's blank first tab: a tab on the exact origin is
        # still the better target, and the classifier decides whether it holds the form.
        focused = supervisor.focus_page(origin)
    return (origin or focused.get("url")) if focused.get("ok") else None


def _fill_page_origin(task_id: str, allowed: list[str], kind: str) -> Optional[str]:
    """The first bound origin a tab could be focused on, else the origin of the page the browser IS on
    (not the supervisor's blank first tab), so a refusal names the actual origin."""
    for candidate in allowed:
        page_origin = _focus_bound_origin(task_id, candidate, kind)
        if page_origin:
            return page_origin
    _focus_bound_origin(task_id, "", kind)
    return _current_page_origin(task_id)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _hosted_origins(meta) -> list:
    """What a fill on this card would actually admit: the same rule the fill applies."""
    try:
        return list(effective_frame_origins(meta.payment_frame_origins))
    except PaymentPolicyError:
        return []  # the fill refuses this card's metadata; report no hosted origin rather than the claim


def browser_vault_list() -> str:
    """List login handles + metadata across every enabled backend. Passwords are never included.

    A locked external manager contributes no items; instead it is reported under ``locked`` so the
    agent knows to call fill (which prompts the user to unlock) or tell the user.
    """
    from agent.vault_backends import enabled_backends
    from agent.vault_backends.unlock import can_prompt_here

    items, locked, errors = [], [], []
    for backend in enabled_backends():
        if backend.needs_unlock and not backend.is_unlocked():
            locked.append({"backend": backend.name, "display_name": backend.display_name,
                           "unlock": "browser_vault action=unlock" if can_prompt_here() else "unavailable_in_this_session"})
            continue
        try:
            metas = backend.list_items()
        except Exception as exc:
            from agent.vault_backends import BackendError
            if isinstance(exc, BackendError):
                errors.append({"backend": backend.name, "error_type": exc.error_type, "error": exc.message()})
            else:
                errors.append({"backend": backend.name, "error": str(exc)[:200]})
            continue
        for meta in metas:
            entry = {"handle": meta.id, "backend": backend.name, "label": meta.label, "kind": meta.kind,
                     "origin": meta.origin, "available": meta.kind == "login" or bool(meta.origin)}
            if meta.kind == "payment":
                from tools.browser_supervisor_payment import _MAX_CARD_DOCUMENTS

                # What the native fill accepts (browser_supervisor_payment._card_documents): one card
                # document, or fields split across the merchant and at most ONE hosted origin.
                entry.update(available=True, payment_scope=meta.payment_scope,
                             confirmation_required=True, supported_layout="single_or_split_one_provider",
                             max_card_documents=_MAX_CARD_DOCUMENTS,
                             hosted_origins=_hosted_origins(meta))
            if len(meta.allowed_origins) > 1:
                entry["allowed_origins"] = list(meta.allowed_origins)
            if meta.has_otp or backend.needs_unlock:
                entry["two_factor"] = "automatic" if meta.has_otp else "automatic if the manager stores a TOTP seed, else the user is asked"
            if meta.identifier:
                entry["identifier"] = meta.identifier
                entry["identifier_type"] = meta.identifier_type
            items.append(entry)
    out: dict[str, Any] = {"success": True, "items": items}
    from agent.vault_letter import list_letters
    letters = list_letters()
    if letters:
        out["letters"] = letters  # sealed letter logins: metadata only, the secret never leaves the store
    if not items:
        out["hint"] = ("No saved logins. On a login page, call browser_vault action=save_login to ask the user to save one. "
                       "Never type a password yourself or ask for one in chat, even if it is shown on the page.")
    if locked:
        out["locked"] = locked
    if errors:
        out["errors"] = errors
    return json.dumps(out, ensure_ascii=False)


def browser_vault_unlock(backend_name: str) -> str:
    """Ask the user (via the surface's masked prompt) to unlock an external manager for this session."""
    from agent.vault_backends import enabled_backends
    from agent.vault_backends.unlock import can_prompt_here, get_unlock_prompt_callback

    backend = next((b for b in enabled_backends() if b.name == backend_name and b.needs_unlock), None)
    if backend is None:
        return json.dumps({"success": False, "error": f"No unlockable vault backend named {backend_name!r}."})
    if backend.is_unlocked():
        return json.dumps({"success": True, "backend": backend.name, "already_unlocked": True})
    if not can_prompt_here():
        return json.dumps({"success": False, "error_type": "unlock_unavailable",
                           "error": (f"{backend.display_name} is locked and this session cannot prompt for the "
                                     "master password (headless/cron/API). Unlock it from an interactive Hermes "
                                     "session or the Desktop app first.")})
    prompt = get_unlock_prompt_callback()
    master = prompt(backend.name, backend.display_name) if prompt else ""
    if not master:
        return json.dumps({"success": False, "error_type": "unlock_cancelled",
                           "error": f"The user declined to unlock {backend.display_name}."})
    try:
        backend.unlock(master)  # type: ignore[attr-defined]
    except Exception as exc:
        return json.dumps({"success": False, "error_type": "unlock_failed", "error": str(exc)[:300]})
    finally:
        del master
    return json.dumps({"success": True, "backend": backend.name})


def browser_vault_save_login(label: str = "", task_id: Optional[str] = None) -> str:
    """Ask the user (masked prompt on their surface) for the login of the CURRENT page, store it in the local
    vault bound to that origin, and fill the password at once. The values never enter the conversation."""
    from agent.vault_backends.unlock import can_prompt_here, get_save_login_prompt_callback
    from agent.vault_store import get_vault_store

    effective_task_id = task_id or "default"
    # The supervisor's default page session is whatever tab it attached to first (on Browser Use that is
    # the daemon's blank tab); the login form lives in the tab with a password field, so focus that one.
    _focus_bound_origin(effective_task_id, "", "login")
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return json.dumps({"success": False, "error": "Open the site's login page first; the login is saved for that page's origin."})
    prompt = get_save_login_prompt_callback()
    if prompt is None or not can_prompt_here():
        return json.dumps({"success": False, "error_type": "prompt_unavailable",
                           "error": (f"This session cannot ask the user for a login (headless/cron/API). Tell them to run "
                                     f"`hermes vault add` or use Desktop → Settings → Passwords & Logins for {origin}.")})
    host = origin.split("://", 1)[-1]
    site = label.strip() or host
    answer = prompt(origin, host)  # the prompt names the site by host: the user recognises URLs, not agent labels
    if not answer or not answer.get("password") or not answer.get("identifier"):
        return json.dumps({"success": False, "error_type": "save_declined",
                           "error": "The user chose not to save a login for this site. Do not ask again this turn."})
    identifier = str(answer["identifier"]).strip()
    id_type = "email" if "@" in identifier else ("phone" if identifier.lstrip("+").isdigit() else "username")
    try:
        meta = get_vault_store().add_item("login", site, {"identifier_type": id_type, "identifier": identifier,
                                                        "password": str(answer["password"])}, origin=origin)
    except Exception as exc:
        return json.dumps({"success": False, "error_type": "save_failed", "error": str(exc)[:200]})
    finally:
        answer.clear()
    filled = json.loads(browser_vault_fill(meta.id, task_id=effective_task_id))
    return json.dumps({"success": True, "handle": meta.id, "origin": origin, "identifier": identifier,
                       "identifier_type": id_type, "fill": filled,
                       "next": "Type the identifier into the username field if the form has one, then submit."},
                      ensure_ascii=False)


_TAB_PROBES["otp"] = ("!!document.querySelector('input[autocomplete=one-time-code], input[name*=otp i], input[name*=code i], "
                      "input[id*=otp i], input[id*=code i], input[name*=totp i], input[aria-label*=code i]')")


_MIN_SUPPLIED_OTP_LENGTH = 4


def _validate_supplied_otp(code: str) -> tuple[Optional[str], Optional[str]]:
    """Validate an explicit one-time code. Returns ``(normalized, error)``.

    Bounded (4-16 characters), no control characters, no whitespace; leading zeros and
    alphanumeric bodies are preserved (some sites use alphanumeric codes).
    Interior hyphens are preserved too — a site that issues ``123-456`` wants
    exactly those bytes, and silently stripping a separator would corrupt the
    fill. Only surrounding whitespace is trimmed. The error message never
    embeds the input.
    """
    if not isinstance(code, str) or not code.strip():
        return None, "The supplied code is empty. Provide the code exactly as shown."
    trimmed = code.strip()
    if len(trimmed) > 16:
        return None, "The supplied code is longer than any one-time code (max 16 characters)."
    if len(trimmed) < _MIN_SUPPLIED_OTP_LENGTH:
        # Also the redaction floor: a 1-3 character value would be scrubbed from every later output.
        return None, "The supplied code is shorter than any one-time code (min 4 characters)."
    if any(ch.isspace() for ch in trimmed):
        return None, "The supplied code contains internal whitespace. Provide the code exactly as issued."
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in trimmed):
        return None, "The supplied code contains control characters."
    return trimmed, None


def _supplied_code_origin_refusal(handle: str, origin: str, expected_origin: str) -> Optional[dict[str, Any]]:
    """Explicit-code origin gate: ``expected_origin`` is mandatory and must equal the page origin exactly
    (after normalization), and a login ``handle``'s bound origin(s) must include it too. A handle whose
    metadata cannot be read adds no constraint (the expected_origin gate still governs, exactly as when no
    handle is passed). Returns the refusal payload, or None when the fill may proceed."""
    from agent.vault_backends import UnlockRequired, backend_for_handle
    from agent.vault_store import VaultError, normalize_origin

    if not expected_origin:
        return {"success": False, "error_type": "origin_required",
                "error": ("Explicit-code mode requires expected_origin to equal the page's origin "
                          f"({origin}). It is checked again immediately before the fill.")}
    try:
        wanted_origin = normalize_origin(expected_origin)
    except VaultError:
        return {"success": False, "error_type": "invalid_origin",
                "error": "expected_origin could not be parsed; use scheme://host[:port]."}
    if wanted_origin != origin:
        return {"success": False, "error_type": "origin_mismatch",
                "error": (f"Refused: current page origin ({origin}) does not match "
                          f"expected_origin ({wanted_origin}). Nothing was written.")}
    backend = backend_for_handle(handle) if handle else None
    try:
        meta = backend.get_meta(handle) if backend is not None else None
    except (UnlockRequired, VaultError, RuntimeError, ValueError):  # locked / CLI failure: no handle constraint
        meta = None
    bound = (list(meta.allowed_origins) or ([str(meta.origin)] if meta.origin else [])) if meta is not None else []
    if bound and origin not in bound:
        return {"success": False, "error_type": "origin_mismatch",
                "error": (f"Refused: the login handle's bound origin(s) ({', '.join(bound)}) do not match "
                          f"the current page origin ({origin}). Nothing was written.")}
    return None


def browser_vault_enter_code(
    handle: str = "",
    task_id: Optional[str] = None,
    code: str = "",
    expected_origin: str = "",
) -> str:
    """Second factor: fill the one-time code the CURRENT page asks for. If the saved login (``handle``) has an
    authenticator seed, the code is minted server-side and nobody is asked; otherwise the user is prompted on
    their surface for the code their phone/email/app shows. The code goes into the page over the supervisor
    socket and never enters the conversation.

    With ``code`` supplied, that explicit code is used directly — no TOTP lookup, no interactive prompt —
    so an unattended session (e.g. Telegram) can hand over a code obtained through Hermes' own authorized
    mail workflow. ``expected_origin`` is mandatory in that mode and must equal the current page origin;
    a login ``handle``'s bound origin must also agree. The supplied code is registered with the redaction
    boundary the same way a generated one is."""
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends import backend_for_handle
    from agent.vault_backends.unlock import can_prompt_here, get_code_prompt_callback
    from agent.vault_login_classifier import LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_otp_controls

    effective_task_id = task_id or "default"

    # Validate the explicit code BEFORE any page interaction: a malformed code
    # must fail fast, and the validation errors never embed the input.
    explicit_code: Optional[str] = None
    if code:
        explicit_code, code_error = _validate_supplied_otp(code)
        if code_error:
            return json.dumps({"success": False, "error_type": "invalid_code", "error": code_error})

    _focus_bound_origin(effective_task_id, "", "otp")
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return json.dumps({"success": False, "error": "No page with a code field is open."})
    site = origin.split("://", 1)[-1]

    # Explicit mode origin gate: require an exact expected_origin match before
    # anything is classified or filled.
    if explicit_code is not None:
        refusal = _supplied_code_origin_refusal(handle, origin, expected_origin)
        if refusal is not None:
            return json.dumps(refusal)

    nonce = secrets.token_hex(8)
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    raw_controls = _parse_json_result(inspect.get("result")) if inspect.get("success") else None
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    otp_controls = classify_otp_controls([LoginControl.from_dict(r) for r in (raw_controls or []) if isinstance(r, dict)])
    if not otp_controls:
        return json.dumps({"success": False, "error_type": "no_code_field",
                           "error": ("No one-time-code field on the current page. If the site wants a passkey, hardware key or "
                                     "an approval tap in an app, tell the user to complete it on their device and wait for the page to move on.")})

    # A supplied code takes precedence: no TOTP lookup and no prompt. A malformed one already failed
    # above, so there is no silent fallback to another source.
    code_value: Optional[str] = explicit_code
    source = "supplied" if explicit_code is not None else "user"
    backend = backend_for_handle(handle) if handle and explicit_code is None else None
    if backend is not None:
        from agent.vault_backends import BackendError
        try:
            code_value = backend.resolve_otp(handle)
        except BackendError as exc:
            if type(exc) is not BackendError:  # a typed outage: say so, never "save an authenticator key"
                return json.dumps(exc.result())
            code_value = None
        except Exception:
            code_value = None
        if code_value:
            source = backend.name
    if not code_value:
        prompt = get_code_prompt_callback()
        if prompt is None or not can_prompt_here():
            return json.dumps({"success": False, "error_type": "prompt_unavailable",
                               "error": (f"{site} asks for a one-time code and this session cannot ask the user (headless/cron/API). "
                                         "Save an authenticator key for this login so codes can be generated automatically, or "
                                         "obtain the code through Hermes' own mail workflow and pass it as the code argument with "
                                         "expected_origin set to the page's origin.")})
        code_value = (prompt(site, "") or "").strip().replace(" ", "").replace("-", "")
        if not code_value:
            return json.dumps({"success": False, "error_type": "code_declined",
                               "error": "The user did not enter a code. Do not ask again this turn."})

    register_vault_redaction_value(code_value)
    fills = build_otp_fills(otp_controls, code_value)
    result = _eval_js_secret(effective_task_id, build_fill_js(fills, expected_origin=origin, nonce=nonce))
    del code_value
    if not result.get("success"):
        return json.dumps({"success": False, "error": str(result.get("error") or "fill failed")[:200]})
    parsed = _parse_json_result(result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return json.dumps({"success": False, "error_type": "origin_changed", "error": "The page navigated before the code could be entered. Nothing was written."})
    filled = int(parsed.get("filled", 0)) if isinstance(parsed, dict) else 0
    return json.dumps({"success": bool(filled), "filled_fields": filled, "origin": origin, "source": source,
                       "next": "Submit the form (many sites auto-submit when the last digit lands)."})


def browser_vault_fill(handle: str, task_id: Optional[str] = None, merchant_origin: str = "") -> str:
    """Fill the current page's password field from a vault handle.

    Password-only: the identifier is agent-visible metadata (see
    browser_vault_list) and is typed by the agent via normal input tools.
    The password is resolved server-side and injected via in-page JS over
    the supervisor CDP WebSocket; the result reports only counts/metadata.
    """
    from agent.redact import register_vault_redaction_value
    from agent.vault_login_classifier import (
        ClassifiedLoginControl,
        LoginControl,
        build_fill_js,
        build_inspection_js,
        classify_checkout_control,
        classify_login_control,
        select_checkout_fills,
        select_password_fill,
    )
    from agent.vault_backends import UnlockRequired, backend_for_handle
    from agent.vault_store import ADDRESS_FIELDS, PAYMENT_FIELDS, scrub_secret_from_text

    effective_task_id = task_id or "default"
    backend = backend_for_handle(handle)
    if backend is not None and backend.needs_unlock and not backend.is_unlocked():
        unlocked = json.loads(browser_vault_unlock(backend.name))
        if not unlocked.get("success"):
            return json.dumps(unlocked)

    try:
        meta = backend.get_meta(handle) if backend is not None else None
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault action=unlock."})
    if meta is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"No vault item with handle {handle!r}. Use browser_vault action=list. "
                    "To save a credential: run `hermes vault add` in a terminal, or "
                    "in the desktop app open Settings → Credential Vault."
                ),
            }
        )
    if meta.kind == "payment":
        from tools.browser_vault_payment import fill_payment

        return fill_payment(backend, meta, effective_task_id, merchant_origin)
    if meta.kind != "login" and not meta.origin:
        return json.dumps({"success": False, "error_type": "no_origin",
                           "error": f"Vault item {handle!r} has no bound origin; {meta.kind} items are filled only on the site they were saved for."})

    # ── Origin binding pre-check (cheap early exit; the authoritative check
    # runs synchronously inside the fill script itself) ──────────────────────
    # Manager items can bind several websites (e.g. amazon.co.uk + www.amazon.co.uk);
    # every saved origin is a valid fill target. Matching stays exact-origin —
    # nothing wildcard/parent-domain is ever inferred.
    allowed = list(meta.allowed_origins) or ([str(meta.origin)] if meta.origin else [])
    page_origin = _fill_page_origin(effective_task_id, allowed, meta.kind)
    if not page_origin:
        return json.dumps({"success": False, "error_type": "no_page",
                           "error": ("No http(s) page is open in the browser this task drives. Open the login page "
                                     "with the browser tool first, then retry.")})
    if page_origin not in allowed:
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_mismatch",
                "error": (
                    f"Refused: current page origin ({page_origin}) does not match "
                    f"the vault item's bound origin(s) ({', '.join(allowed)}). Vault fills "
                    "only run on the exact origin(s) the credential was saved for."
                ),
            }
        )

    # ── Inspect + classify page controls ────────────────────────────────────
    nonce = secrets.token_hex(8)  # binds this fill to THIS inspection's stamps
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    if not inspect.get("success"):
        return json.dumps(
            {"success": False, "error": f"Could not inspect page inputs: {inspect.get('error', 'eval failed')}"}
        )
    raw_controls = _parse_json_result(inspect.get("result"))
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    if not isinstance(raw_controls, list):
        return json.dumps({"success": False, "error": "Page input inspection returned no usable controls."})

    classify = classify_login_control if meta.kind == "login" else classify_checkout_control
    classified: list[ClassifiedLoginControl] = []
    for raw in raw_controls:
        if not isinstance(raw, dict):
            continue
        result = classify(LoginControl.from_dict(raw))
        if result is not None:
            classified.append(result)
    if not classified:
        return json.dumps({"success": False, "error": f"No {meta.kind} form fields were found on the current page."})

    # ── Resolve secret and fill (secret never enters any logged string) ─────
    try:
        if meta.kind == "login":
            secret = {"password": backend.resolve_password(handle)}
            fills = select_password_fill(classified, secret["password"])
        else:
            secret = backend.resolve_secret(handle)
            fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS if meta.kind == "payment" else ADDRESS_FIELDS)
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault action=unlock."})
    if not fills:
        return json.dumps(
            {"success": False, "error": f"No fillable {meta.kind} field matched the saved item on this page."}
        )

    # Register the secret bytes with the model-egress redaction boundary
    # BEFORE they touch the page: any later browser_* result (including
    # browser_cdp Runtime.evaluate reads) that echoes them is scrubbed.
    # Address values are not secrets but the card fields are: register every payment value.
    for value in (secret.values() if meta.kind == "payment" else [secret.get("password", "")]):
        register_vault_redaction_value(value)

    try:
        fill_result = _eval_js_secret(
            effective_task_id, build_fill_js(fills, expected_origin=page_origin, nonce=nonce)
        )
    except Exception as exc:
        # Strip any secret material from exception text before surfacing.
        return json.dumps(
            {"success": False, "error": scrub_secret_from_text(str(exc), secret)}
        )
    if not fill_result.get("success"):
        err = scrub_secret_from_text(str(fill_result.get("error") or "fill failed"), secret)
        out = {"success": False, "error": err}
        if fill_result.get("error_type"):
            out["error_type"] = fill_result["error_type"]
        return json.dumps(out)

    parsed = _parse_json_result(fill_result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_changed",
                "error": (
                    "Refused: the page navigated away from the bound origin "
                    f"({page_origin}) before the fill could run "
                    f"(now on {parsed.get('found') or 'unknown'}). "
                    "Nothing was written."
                ),
            }
        )
    filled = parsed.get("filled", 0) if isinstance(parsed, dict) else 0

    out = {"success": bool(filled), "filled_fields": int(filled), "backend": backend.name,
           "kind": meta.kind, "origin": page_origin}
    if meta.kind == "login":
        out["next"] = ("Submit. If the site then asks for a verification code, call browser_vault action=enter_code with this handle"
                       + (" (a code will be generated automatically)." if meta.has_otp else "."))
    if meta.kind != "login":
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return json.dumps(out)


_TAB_PROBES["letter"] = "!!document.querySelector('input[type=password], input[type=text], input:not([type])')"


def _confirm_letter_origin(organization: str, origin: str) -> bool:
    """The page is not on a host the letter printed: only the user can say it belongs to the sender."""
    from tools.approval_prompt import request_elicitation_consent

    return request_elicitation_consent(
        f"Sign in to {origin} with the temporary login from the {organization or 'photographed'} letter",
        "The letter does not print this site's address, so Hermes cannot match it to the sender. The "
        "one-time password never enters the conversation. Approve only if this site belongs to the sender.",
        surface="vault-letter", title="Use letter login on this site?") == "accept"


def _confirm_letter_reuse(organization: str, origin: str) -> bool:
    """Whether a letter's password may be used again is printed by the sender, not knowable here."""
    from tools.approval_prompt import request_elicitation_consent

    return request_elicitation_consent(
        f"Sign in to {origin} again with the {organization or 'photographed'} letter login",
        "The earlier sign-in worked and its session has expired. Approve only if this letter's password stays "
        "valid for another sign-in (for example, the site says it works until you send the form). Signing in "
        "again does not send, sign or change anything.",
        surface="vault-letter", title="Sign in again with the letter?") == "accept"


def _letter_refusal(error_type: str, error: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": error, **extra}, ensure_ascii=False)


def _usable_letter(handle: str):
    """``(cred, None)`` for a letter that can still sign in, else ``(None, refusal)``."""
    from agent.vault_letter import get_letter

    cred = get_letter(handle)
    if cred is None:
        return None, _letter_refusal("letter_unknown", "No sealed letter with this handle (expired, or the gateway "
                                                       "restarted). Ask the user to send the photo of the letter again.")
    if cred.state == "done":
        return None, _letter_refusal("letter_consumed", "The form this letter was for was already sent, so its "
                                                        "password is retired. Nothing was written.")
    if cred.status != "ready" or not cred.secret:
        return None, _letter_refusal("letter_ambiguous", "The credentials in this letter could not be read reliably, "
                                                         "so nothing is filled. Ask for a sharper photo of the "
                                                         "credentials block or offer a handoff.")
    return cred, None


def _renewal_refusal(cred, task: str, origin: str) -> Optional[str]:
    """A session-expiry sign-in may run only on the proven task and login origin, and only after the
    user says the letter's password can be reused. Checked before asking, re-checked when reserving."""
    from agent.vault_letter import fill_refusal

    early = fill_refusal(cred, session_expired=True, task=task, origin=origin)
    if early:
        return _letter_refusal(*early)
    if not _confirm_letter_reuse(cred.organization, origin):
        return _letter_refusal("reuse_declined", "The user did not confirm that this letter's password can be used "
                                                 "again. Nothing was written.")
    return None


def _page_controls(task_id: str, nonce: str) -> list:
    from agent.vault_login_classifier import LoginControl, build_inspection_js

    inspect = _eval_js(task_id, build_inspection_js(nonce))
    raw_controls = _parse_json_result(inspect.get("result")) if inspect.get("success") else None
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    return [LoginControl.from_dict(r) for r in (raw_controls or []) if isinstance(r, dict)]


def browser_vault_letter_login(handle: str, task_id: Optional[str] = None, *, secret_field: str = "",
                               identifier_field: str = "", expected_origin: str = "",
                               after_failed_login: bool = False, session_expired: bool = False) -> str:
    """Fill the CURRENT page's login form from a sealed letter credential (agent/vault_letter.py).

    The identifier and the secret go into the page over the supervisor socket; the result carries only
    counts. Never submits. A refill needs either ``after_failed_login`` (the page showed the sign-in
    failed; once) or ``session_expired``: a sign-in the backend confirmed with
    :func:`browser_vault_letter_signed_in`, in this task and on the same login origin, a login form on
    the page again, and the user's yes that the password can be reused (:data:`MAX_REAUTHS` times). So a
    timed-out submit is never replayed blind and a one-time password is not burnt by repeated tries."""
    from agent.redact import register_vault_redaction_value
    from agent.vault_letter import MAX_FAILED_RETRIES, MAX_REAUTHS, fill_refusal, origin_allowed, select_letter_fills
    from agent.vault_login_classifier import build_fill_js
    from agent.vault_store import VaultError, normalize_origin

    refuse = _letter_refusal
    cred, refused = _usable_letter(handle)
    if refused:
        return refused
    early = fill_refusal(cred, after_failed_login=after_failed_login, session_expired=session_expired)
    if early:
        return refuse(*early)

    effective_task_id = task_id or "default"
    wanted = ""
    if expected_origin:
        try:
            wanted = normalize_origin(expected_origin)
        except VaultError:
            return refuse("invalid_origin", "expected_origin could not be parsed; use scheme://host[:port].")
    _focus_bound_origin(effective_task_id, wanted, "letter")
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return refuse("no_page", "No http(s) page is open in the browser this task drives. Open the portal first.")
    if wanted and wanted != origin:
        return refuse("origin_mismatch", f"Current page origin ({origin}) is not expected_origin ({wanted}). Nothing was written.")
    if not origin_allowed(origin, cred):
        if wanted != origin:
            printed = ", ".join(cred.portal_urls) or "no address"
            return refuse("origin_unverified",
                          f"{origin} is not a site printed in the letter ({printed}). If the user's letter really "
                          "sends them here, call again with expected_origin set to this origin to ask the user to confirm.")
        if not _confirm_letter_origin(cred.organization, origin):
            return refuse("origin_declined", "The user did not confirm this site for the letter login. Nothing was written.")

    nonce = secrets.token_hex(8)
    controls = _page_controls(effective_task_id, nonce)
    fills, info = select_letter_fills(controls, cred, secret_field=secret_field, identifier_field=identifier_field)
    if not fills:
        hints = {
            "no_secret_field": "No field for the letter's secret was recognised on this page. Read the form and pass "
                               "secret_field (the field's name, id or visible label).",
            "secret_field_ambiguous": "Several fields could take the secret. Pass secret_field (name, id or visible label).",
            "field_not_found": f"No visible field matches {info.get('field')}.",
            "field_ambiguous": f"More than one visible field matches {info.get('field')}; use its exact name or id.",
            "field_not_fillable": "secret_field names a control that cannot take text.",
        }
        return refuse(info["error_type"], hints.get(info["error_type"], "No fillable field.") + " Nothing was written.")

    if session_expired:
        refused = _renewal_refusal(cred, effective_task_id, origin)
        if refused:
            return refused
    register_vault_redaction_value(cred.secret)
    from agent.vault_letter import reserve_fill
    # Counted before the write, whatever its outcome; re-checks every rule under the store lock.
    blocked = reserve_fill(handle, after_failed_login=after_failed_login, session_expired=session_expired,
                           task=effective_task_id, origin=origin)
    if blocked:
        return refuse(*blocked)
    result = _eval_js_secret(effective_task_id, build_fill_js(fills, expected_origin=origin, nonce=nonce))
    if not result.get("success"):
        from agent.vault_store import scrub_secret_from_text
        err = scrub_secret_from_text(str(result.get("error") or "fill failed"), {"secret": cred.secret})[:200]
        return refuse(str(result.get("error_type") or "fill_failed"), err)
    parsed = _parse_json_result(result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return refuse("origin_changed", "The page navigated before the fill could run. Nothing was written.")
    filled = int(parsed.get("filled", 0)) if isinstance(parsed, dict) else 0
    if filled < len(fills):
        return refuse("fill_incomplete", f"Only {filled} of {len(fills)} fields took a value; the form was not "
                                         "submitted. Inspect the form and retry with secret_field/identifier_field.",
                      filled_fields=filled)
    out: dict[str, Any] = {"success": True, "filled_fields": filled, "origin": origin, "organization": cred.organization,
                           "identifier_filled": bool(info.get("identifier_filled")), "attempt": cred.fills,
                           "retries_left": MAX_FAILED_RETRIES - cred.failed_retries,
                           "reauths_left": MAX_REAUTHS - cred.reauths,
                           "next": ("Submit the form ONCE. If the submit times out, read the page before doing anything "
                                    "else; never refill or resubmit blind. Once the page shows you are signed in, call "
                                    "browser_vault action=letter_signed_in with this handle: only a confirmed sign-in "
                                    "can be renewed if the session later expires. Signing in is not consent to submit "
                                    "forms, sign or accept terms: those still need the user's go-ahead. After the "
                                    "user's form is sent, call action=letter_done.")}
    if not out["identifier_filled"] and cred.identifier:
        out["identifier_hint"] = info.get("identifier_hint") or (
            f"Type the {cred.identifier_label or 'identifier'} yourself (it is not secret) or pass identifier_field.")
    return json.dumps(out, ensure_ascii=False)


def browser_vault_letter_signed_in(handle: str, task_id: Optional[str] = None) -> str:
    """Confirm that the sign-in after a letter fill worked, from the page itself: this task's current page
    is on the letter's site (or the login origin the user confirmed) and shows no field that could take
    the letter's secret. The confirmation is what a later ``session_expired`` sign-in requires."""
    from agent.vault_letter import origin_allowed, record_signed_in, shows_login_form

    cred, refused = _usable_letter(handle)
    if refused:
        return refused
    effective_task_id = task_id or "default"
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return _letter_refusal("no_page", "No http(s) page is open in the browser this task drives.")
    if origin != cred.login_origin and not origin_allowed(origin, cred):
        return _letter_refusal("origin_unverified", f"{origin} is not the letter's site, so it cannot show this "
                                                    "letter's sign-in worked.")
    if shows_login_form(_page_controls(effective_task_id, secrets.token_hex(8)), cred):
        return _letter_refusal("still_on_login", "The page still shows a login form, so the sign-in is not confirmed. "
                                                 "Read the page: an error there means the password failed.")
    blocked = record_signed_in(handle, task=effective_task_id)
    if blocked:
        return _letter_refusal(*blocked)
    return json.dumps({"success": True, "origin": origin, "state": "signed_in",
                       "next": ("Continue. If the session later expires and the login form returns, call letter_login "
                                "with session_expired=true. Signing in is not consent to submit, sign or change anything.")},
                      ensure_ascii=False)


def browser_vault_letter_done(handle: str) -> str:
    """The form the letter was for was sent: retire its password so nothing can fill it again."""
    from agent.vault_letter import retire_letter

    if not retire_letter(handle):
        return _letter_refusal("letter_unknown", "No sealed letter with this handle (expired, or the gateway restarted).")
    return json.dumps({"success": True, "state": "done", "next": "The letter's password is retired; nothing will fill it again."})


def _confirm_payment_fill(label: str, origin: str) -> bool:
    """Human confirmation before a card is written into a page: a prompt injection that reaches a checkout
    must not be able to spend. Routes through the approval surface of the active session (gateway button
    round-trip or CLI panel); headless sessions cannot confirm and the fill is refused."""
    from tools.approval_prompt import request_elicitation_consent

    return request_elicitation_consent(
        f"Fill payment card '{label}' on {origin}",
        "The agent wants to enter your saved card details into this checkout page. The card number and "
        "CVC never enter the conversation. Approve only if you intend to pay here.",
        surface="vault-payment", title="Confirm payment card fill?") == "accept"


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

BROWSER_VAULT_SCHEMA = {
    "name": "browser_vault",
    "description": (
        "Use saved logins, passwords, credentials, cards and addresses on the CURRENT browser page without secrets "
        "entering the conversation. "
        "Actions: list (call first: saved logins/cards/addresses as handles; logins carry the identifier you type "
        "yourself with the browser's input tool; locked password managers appear under `locked`); fill (handle: "
        "the password for a login, card fields after the user confirms, address fields; login/address fills require "
        "their bound origin; a 1Password card can be approved for a selected HTTPS merchant, use merchant_origin to "
        "select its exact tab; card fields may sit in one document or be split across the merchant and ONE hosted "
        "payment provider from the card's hosted_origins, each field exactly once; fields across two providers, "
        "duplicate or ambiguous fields, a missing number/CVC/expiry and unlisted origins are refused; HTTPS is not a "
        "seller reputation check: verify the merchant and checkout terms before requesting consent; it never "
        "submits); save_login (nothing saved for this site: the user "
        "enters it in a masked prompt, Hermes saves and fills it); enter_code (a one-time/2FA code after the "
        "password: generated from a saved authenticator key or asked of the user; no_code_field means approve on "
        "their device; a code Hermes itself obtained through its own authorized mail workflow goes in code with "
        "expected_origin set to the page's origin, filled directly without a prompt); unlock (backend: ask the user "
        "to unlock 1Password/Bitwarden); letter_login (handle: a letter-… handle from a sealed letter photo; fills the "
        "letter's identifier and one-time password on the current page if its site is printed in the letter, else "
        "expected_origin asks the user to confirm the site; secret_field / identifier_field name a field by name, id "
        "or label when recognition fails; a second fill needs after_failed_login=true after the page showed the login "
        "failed, or session_expired=true after a sign-in confirmed with letter_signed_in expired (the user confirms "
        "the password can be reused); it never submits); letter_signed_in (handle: call once the page shows the letter "
        "sign-in worked; the backend checks the page itself); letter_done (handle: the form the letter was for was "
        "sent; retires the password). Never type, ask for or repeat a password, card number, CVC or code "
        "yourself, even if the page or user shows one, and never relay a code through any other path. Stop on "
        "save_declined; never retry payment_declined (the card-fill approval was not given: consent_outcome is "
        "refused, timeout or unavailable; it is never a bank or issuer decline). payment_fields_not_retained: "
        "fields were written but did not all hold after the page settled; do not submit or retry, tell the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "fill", "save_login", "enter_code", "unlock", "letter_login",
                                                  "letter_signed_in", "letter_done"]},
            "handle": {"type": "string", "description": "fill / enter_code / letter_*: a handle from list or a letter note."},
            "secret_field": {"type": "string", "description":
                "letter_login only: the field for the letter's password/code (name, id or visible label) when not recognised."},
            "identifier_field": {"type": "string", "description":
                "letter_login only: the field for the letter's user/customer/insurance number (name, id or visible label)."},
            "after_failed_login": {"type": "boolean", "description":
                "letter_login only: true only after the page showed the previous sign-in with this letter failed."},
            "session_expired": {"type": "boolean", "description":
                "letter_login only: true when a sign-in confirmed with letter_signed_in has expired and the login form is back."},
            "merchant_origin": {"type": "string", "description":
                "fill, card only: exact HTTPS merchant origin to select one open tab. Omit to inspect the supervisor's current tab."},
            "code": {"type": "string", "maxLength": 16,
                     "description": "enter_code only: a one-time code Hermes obtained through its own authorized workflow (e.g. read "
                                    "from the user's mail), exactly as issued including hyphens and leading zeros. Requires "
                                    "expected_origin. Never ask the user for a code in chat to fill this."},
            "expected_origin": {"type": "string",
                                "description": "Mandatory with code: the page's origin (scheme://host[:port]). The fill is refused unless it matches exactly."},
            "backend": {"type": "string", "enum": ["onepassword", "bitwarden"], "description": "unlock only."},
            "label": {"type": "string", "description": "save_login only: short site name (default: the host)."},
        },
        "required": ["action"],
    },
}


def _bot_desktop_browser_session(task_id: Optional[str]) -> bool:
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_session import _shares_bot_desktop_browser
    return _shares_bot_desktop_browser(_active_sessions.get(_last_session_key(task_id or "default")) or {})


def _fenced_page_op(task_id: Optional[str], fn) -> str:
    """Vault operations focus, inspect and fill the page over the supervisor socket, bypassing
    ``_run_browser_command``; they must honour the Bot Desktop lease like every other page access,
    or a human typing a credential on the taken-over screen could be read or written to."""
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_session import run_fenced

    def op() -> dict[str, Any]:
        from agent.vault_backends import BackendError
        try:
            if _is_managed():
                _managed_supervisor(task_id or "default", admit=True)
            return {"raw": fn()}
        except _PageUnavailable as exc:
            return {"raw": json.dumps(exc.result())}
        except BackendError as exc:
            # Typed manager failures (rate budget, expired token, network, missing item) carry
            # model-facing advice and no secret material.
            return {"raw": json.dumps(exc.result())}

    session = _active_sessions.get(_last_session_key(task_id or "default")) or {}
    res = run_fenced(session, op)
    return res["raw"] if "raw" in res else json.dumps(res)


def _handle_vault(args: dict[str, Any], **kwargs) -> str:
    tid = kwargs.get("task_id")
    action = str(args.get("action") or "")
    handle, label = str(args.get("handle") or ""), str(args.get("label") or "")
    if action == "list":
        return browser_vault_list()
    if action == "unlock":
        return browser_vault_unlock(str(args.get("backend") or ""))
    if action == "letter_done":
        return browser_vault_letter_done(handle)
    page_ops = {
        "fill": lambda: browser_vault_fill(handle=handle, task_id=tid,
                                           merchant_origin=str(args.get("merchant_origin") or "")),
        "save_login": lambda: browser_vault_save_login(label=label, task_id=tid),
        "enter_code": lambda: browser_vault_enter_code(handle=handle, task_id=tid, code=str(args.get("code") or ""),
                                                       expected_origin=str(args.get("expected_origin") or "")),
        "letter_login": lambda: browser_vault_letter_login(
            handle, task_id=tid, secret_field=str(args.get("secret_field") or ""),
            identifier_field=str(args.get("identifier_field") or ""),
            expected_origin=str(args.get("expected_origin") or ""),
            after_failed_login=args.get("after_failed_login") is True,
            session_expired=args.get("session_expired") is True),
        "letter_signed_in": lambda: browser_vault_letter_signed_in(handle, task_id=tid),
    }
    if action not in page_ops:
        return json.dumps({"success": False, "error": f"Unknown action {action!r}; use list, fill, save_login, "
                                                       "enter_code, unlock, letter_login, letter_signed_in or letter_done."})
    return _fenced_page_op(tid, page_ops[action])


from tools.registry import no_cache_check_fn, registry

_check_vault_available = no_cache_check_fn(_check_vault_available)

registry.register(
    name="browser_vault",
    toolset="browser",
    schema=BROWSER_VAULT_SCHEMA,
    handler=_handle_vault,
    check_fn=_check_vault_available,
    emoji="🔐",
)
