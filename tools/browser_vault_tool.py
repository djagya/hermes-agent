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
  exclusively over the supervisor CDP WebSocket (never argv) — or, for a plugin browser with no CDP
  endpoint, through its registered ``BrowserPageBackend`` (Camofox's REST evaluate) — and the tool
  result reports only ``{filled_fields, kind, origin, success}`` — the
  password never appears in tool results, logs, or the session DB, and its
  exact bytes are registered with the browser-result redaction boundary so
  no later browser tool call can echo them back to the model.
- ``enter_code`` → a one-time code for the current page: minted from a saved
  login's authenticator key (same origin binding as ``fill``, checked before
  the code is generated) or typed by the user into their surface's prompt.

Ported design from Merit-Systems/OpenInstinct (MIT): opaque-handle vault
autofill (kernel-login-autofill.ts / fill_from_vault.ts).
"""

from __future__ import annotations

import contextvars
import json
import secrets
import logging
from typing import Any, Dict, Optional
from agent.vault_payment_policy import PaymentPolicyError, destination_consent_enabled, effective_frame_origins

from tools.browser_vault_context import (fill_context, plugin_errors_as_refusal, saved_origins, snapshot_refusal,
                                         write_backend_code)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_vault_available() -> bool:
    """Schema-gate: the vault rides with the browser. An empty vault still needs save_login so the
    agent can offer to remember a login the first time it meets a form; hiding the tool until an item
    exists meant nobody ever discovered the feature. Deferral keeps that presence nearly free."""
    from agent.browser_page_registry import list_providers
    from tools.browser_session_cdp import get_cdp_override_raw
    from tools.browser_use_cli import browser_exec_available
    return bool(browser_exec_available() or get_cdp_override_raw() or list_providers())


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
    """dlz: monolith browser-control mode. An unimportable route module fails closed (managed): the
    managed path then refuses with a typed result instead of falling through to a local/plugin browser."""
    try:
        from tools.browser_control_route import is_managed
        return is_managed()
    except Exception:  # health: allow BLE001 -- a broken import or probe must not look unmanaged
        logger.exception("vault: managed-browser probe failed; treating the browser as managed")
        return True


_PREVIEW_MANAGED_ERROR = ("The in-app browser is not used while Hermes drives the managed browser: open the page "
                          "with browser_exec and call browser_vault with target=browser. Nothing was read or written.")
# dlz: the in-app browser runs only the upstream preview operations; card, letter and payment-lock
# actions (payment_controls / payment_click / payment_done / payment_dialog) stay on the managed browser's fences.
_PREVIEW_ACTIONS = ("fill", "save_login", "enter_code")


def _preview_refusal() -> Optional[str]:
    """dlz: managed mode refuses every in-app (preview) vault operation before any page access."""
    if not _is_managed():
        return None
    return json.dumps({"success": False, "error_type": "preview_unavailable", "error": _PREVIEW_MANAGED_ERROR})


def _managed_supervisor(task_id: str, *, admit: bool = False):
    """Managed mode: the supervisor on the browser browser_exec last drove for this task.

    Never acquires: a fresh lease is a different browser without the page, and the legacy session
    path acquires without the exec's identity. A dropped socket is re-attached to the same lease after
    control confirms the agent still holds it; ``admit`` asks control even when the socket looks live
    (start of every vault page operation, and again right before a secret is written)."""
    from tools.browser_vault_preview import active_page
    if active_page() is not None:
        # dlz: a vault operation pinned to the Desktop in-app page must never land on the leased slot.
        raise _PageUnavailable("preview_unavailable", _PREVIEW_MANAGED_ERROR)
    try:
        from tools.browser_control_route import _lease_alive, control_url
    except ImportError:
        raise _PageUnavailable("unavailable", "The managed browser route is unavailable in this install. "
                               "Nothing was read or written.") from None
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
    from tools.browser_session_cdp import get_dialog_policy_config, resolve_cdp_override
    policy, timeout_s = get_dialog_policy_config()
    try:
        return SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=resolve_cdp_override(route["cdp"]),
                                                dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:  # health: allow BLE001 -- re-raised as _PageUnavailable; a traceback would leak the CDP endpoint
        cause = _redact_cdp_error_text(exc)[:200]
        logger.warning("vault: supervisor attach to the managed browser failed: %s", cause)
        raise _PageUnavailable("supervisor_unavailable",
                               f"Could not open the vault's CDP connection to the managed browser: {cause}") from None


# A secret-bearing script runs inside this wrapper on EVERY transport (CDP supervisor and plugin page
# backends), so a page exception is answered in-page and its message (which may quote the script or, after
# a write, carry the value a page handler read back) never reaches a backend's error path or logs.
# Every answer is a string primitive: an object (the old sentinel, or anything a hooked JSON.stringify
# returns) would be assimilated through a page-defined ``then`` outside any catch. An async script is
# observed with the Promise intrinsics captured BEFORE it runs: its value or rejection lands on the wrapper's
# own promise, never passing through a page-reachable ``then``/``await``. Strict mode (inherited by the
# wrapped script): a page function the script calls gets ``.caller === null``, never the secret-bearing source.
_PAGE_FILL_FAILED = "hermes_vault_eval_failed"


def _secret_wrapped(expression: str) -> str:
    failed = json.dumps(_PAGE_FILL_FAILED)
    return ("(() => { \"use strict\"; const F = " + failed + "; const P0 = Promise, then0 = Promise.prototype.then, "
            "apply = Reflect.apply; let r; try { r = (" + expression + "); } catch (_) { return F; } "
            "if (typeof r === \"string\") return r; let settle = null; "
            "try { const done = new P0((ok) => { settle = ok; }); "
            "apply(then0, r, [(v) => { settle(typeof v === \"string\" ? v : F); }, () => { settle(F); }]); "
            "return done; } catch (_) { return F; } })()")


def _page_backend(task_id: str):
    """The page this operation runs on when it is not the task's CDP session: the Desktop in-app browser
    page pinned for a ``target='preview'`` operation, else the plugin browser holding this task's live page
    (e.g. Camofox), else None.

    dlz: never in managed mode: the leased slot is the only browser, and a plugin or in-app page is
    outside control's fence (capture guard, lease admission). A pinned preview page is refused there
    (:func:`_preview_refusal`, :func:`_managed_supervisor`), never silently swapped for the slot."""
    if _is_managed():
        return None
    from agent.browser_page_registry import backend_for_task
    from tools.browser_vault_preview import active_page
    return active_page() or backend_for_task(task_id)


def _page_eval(backend: Any, task_id: str, expression: str, *, secret: bool) -> dict[str, Any]:
    """Evaluate on a plugin page backend. On the secret path every failure gets fixed text (the caller,
    _eval_js_secret, has already wrapped the expression with _secret_wrapped)."""
    try:
        result = backend.evaluate(task_id, expression, secret=secret)
    except Exception as exc:  # health: allow BLE001 -- plugin failure becomes a typed refusal
        detail = "" if secret else f": {str(exc)[:200]}"
        return {"success": False, "error_type": "eval_failed",
                "error": f"The {backend.name} browser could not run the vault script{detail}"}
    if secret and _parse_json_result(result) == _PAGE_FILL_FAILED:
        return {"success": False, "error_type": "eval_failed",
                "error": f"The {backend.name} browser could not run the vault script"}
    return {"success": True, "result": result}


def _eval_js(task_id: str, expression: str) -> dict[str, Any]:
    """Evaluate NON-SECRET JS on the current page (inspection, origin reads): on the plugin browser
    holding the task's page, else over the task's CDP supervisor. Without either the call reports it.

    dlz: managed mode reads only the supervisor on the leased slot browser_exec last drove
    (:func:`_managed_supervisor`; raises :class:`_PageUnavailable`)."""
    if _is_managed():
        managed = _managed_supervisor(task_id)
        sup = _native(managed, lambda: managed.evaluate_runtime(expression))
        if sup.get("ok"):
            return {"success": True, "result": sup.get("result")}
        return {"success": False, "error": str(sup.get("error") or "eval failed")}
    page = _page_backend(task_id)
    if page is not None:
        return _page_eval(page, task_id, expression, secret=False)
    supervisor = _ensure_supervisor(task_id)
    if supervisor is None:
        return {"success": False, "error": "no supervised browser session for this task; open the page with "
                                           "browser_exec first"}
    sup = _native(supervisor, lambda: supervisor.evaluate_runtime(expression))
    if sup.get("ok"):
        return {"success": True, "result": sup.get("result")}
    return {"success": False, "error": str(sup.get("error") or "evaluate failed")}


def _ensure_supervisor(task_id: str):
    """The supervisor for ``task_id`` (browser_exec attaches one on every call), attaching one on demand to
    the task's session CDP endpoint when none is running. None when no endpoint is known; a fill then
    refuses rather than touching argv. Managed mode raises :class:`_PageUnavailable` instead
    (see :func:`_managed_supervisor`)."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    if _is_managed():
        return _managed_supervisor(task_id)
    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is not None:
        return supervisor
    from tools.browser_session import session_for_task
    from tools.browser_session_cdp import get_dialog_policy_config, resolve_cdp_override

    cdp_url = str(session_for_task(task_id).get("cdp_url") or "")
    if not cdp_url:
        return None
    policy, timeout_s = get_dialog_policy_config()
    try:
        return SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=resolve_cdp_override(cdp_url),
                                                dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:  # health: allow BLE001 -- a failed attach is reported as supervisor_required
        logger.debug("vault fill: supervisor attach to the task's session failed (%s)", exc)
        return None


def _eval_js_secret(task_id: str, expression: str) -> dict[str, Any]:
    """Evaluate a SECRET-BEARING JS expression. Supervisor CDP-WS only.

    Fails closed: there is deliberately NO subprocess path, because that would
    place the expression — and therefore the credential bytes — in argv,
    visible to any process listing. When no supervisor session is available
    the caller gets a typed refusal (``error_type='supervisor_required'``) and
    nothing is written. A plugin browser holding the task's page (no CDP endpoint) carries the
    expression over its own transport instead; see ``agent.browser_page_backend``.
    """
    expression = _secret_wrapped(expression)
    page = _page_backend(task_id)  # always None in managed mode
    supervisor = None
    if page is None:
        try:
            # dlz: managed mode re-admits the lease with control right before a secret is written.
            supervisor = _managed_supervisor(task_id, admit=True) if _is_managed() else _ensure_supervisor(task_id)
        except _PageUnavailable as exc:
            return exc.result()
        except Exception as exc:
            logger.debug("vault fill: supervisor unavailable (%s)", exc)

    if page is None and supervisor is None:
        return {
            "success": False,
            "error_type": "supervisor_required",
            "error": (
                "Vault fill requires the supervised browser session (direct "
                "CDP WebSocket), and this task has none. Open the page with "
                "browser_exec and retry."
            ),
        }

    # Re-admit at the WRITE. The handler-level fence (_fenced_page_op) admitted before a possibly
    # human-length prompt (enter_code waits for the user's code); a takeover during that wait must
    # refuse here, before the credential lands in a page the human is now typing into. The outer
    # epoch check only discards the result, and a fill is a side effect, not a result.
    from tools.browser_vault_preview import active_page
    if active_page() is None and _bot_desktop_browser_session(task_id):
        from tools.bot_desktop import lease as _bd_lease
        try:
            _bd_lease.assert_agent_may_act()
        except _bd_lease.HumanHasControl as exc:
            return {"success": False, "error_type": "human_has_control", "error": str(exc)}

    if page is not None:
        return _page_eval(page, task_id, expression, secret=True)
    sup = _native(supervisor, lambda: supervisor.evaluate_runtime(expression))
    if sup.get("ok") and _parse_json_result(sup.get("result")) == _PAGE_FILL_FAILED:
        return {"success": False, "error_type": "eval_failed", "error": "The browser could not run the vault script"}
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


def _deep_probe(selector: str) -> str:
    """A JS expression truthy when ``selector`` matches in the document or any open shadow root (the
    inspection and fill scripts reach shadow-root forms, so tab selection must too)."""
    return ("((s) => { const w = (r) => r.querySelector(s) || Array.from(r.querySelectorAll('*'))"
            ".some((e) => e.shadowRoot && w(e.shadowRoot)); return !!w(document); })(" + json.dumps(selector) + ")")


# Per kind: a JS probe that is truthy on a tab holding the form this kind fills.
_TAB_PROBES = {
    "login": _deep_probe("input[type=password]"),
    "payment": _deep_probe("input[autocomplete^=cc-], [name*=card i], [placeholder*=card i], [name*=cvc i], [name*=cvv i]"),
    "address": _deep_probe("input[autocomplete^=address-], [autocomplete=postal-code], [name*=address i], [name*=zip i], [name*=postal i]"),
}


def _focus_bound_origin(task_id: str, origin: str, kind: str) -> Optional[str]:
    """Point the supervisor's page session at the open tab on ``origin`` that holds a ``kind`` form
    (browser_exec sessions open their own tabs, so the tab the supervisor attached to first is rarely the
    login page). Returns the origin when a tab was focused, else None (caller falls back to the current page).
    A plugin page backend's current page IS the tab the agent drove, so there is nothing to focus."""
    if _page_backend(task_id) is not None:
        return None
    try:
        supervisor = _ensure_supervisor(task_id)
    except _PageUnavailable:
        raise
    except Exception:
        supervisor = None
    if supervisor is None:
        return None
    focused = _native(supervisor, lambda: supervisor.focus_page(origin, accept=_TAB_PROBES.get(kind)))
    if not focused.get("ok") and not _fence_refused():
        # The probe reads only the top document, so a field in a shadow root or an iframe is invisible to it.
        # The supervisor's previous page is usually the exec's blank first tab: a tab on the exact origin is
        # still the better target, and the classifier decides whether it holds the form.
        focused = _native(supervisor, lambda: supervisor.focus_page(origin))
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


def _bound_page_origin(task_id: str, meta: Any, probe_kind: str) -> str | dict[str, Any]:
    """The origin guard every fill of a stored secret (password, one-time code, card, address) passes
    BEFORE the backend resolves any value: the page origin when it is one of the item's saved origins,
    else the refusal to return. Manager items may bind several websites; matching is exact and
    nothing wildcard or parent-domain is inferred. The caller hands the returned origin to ``build_fill_js``,
    which re-asserts it inside the page immediately before the write (navigation between check and fill)."""
    allowed = saved_origins(meta)
    if not allowed:
        return {"success": False, "error_type": "no_origin",
                "error": f"Vault item {meta.id!r} has no bound origin; {meta.kind} items are filled only on "
                         "the site they were saved for."}
    # dlz: focus a bound-origin tab first, else read the page the browser is actually on (_fill_page_origin).
    page_origin = _fill_page_origin(task_id, allowed, probe_kind)
    if not page_origin:
        return {"success": False, "error_type": "no_page",
                "error": ("No http(s) page is open in the browser this task drives. Open the login page "
                          "with the browser tool first, then retry.")}
    if page_origin not in allowed:
        return {"success": False, "error_type": "origin_mismatch",
                "error": (f"Refused: current page origin ({page_origin}) does not match the vault item's bound "
                          f"origin(s) ({', '.join(allowed)}). Vault fills only run on the exact origin(s) the "
                          "credential was saved for.")}
    return page_origin


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
                             hosted_origins=_hosted_origins(meta),
                             # dlz (Track B): hosted_origins stays the reviewed set; an unreviewed hosted form can
                             # only be filled per fill, after the owner's own approval of its exact origins.
                             destination_consent="per_fill_owner_approval" if destination_consent_enabled()
                             else "off")
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


# dlz: sessions with no human chat surface (or with their own masked prompt): the B20 chat text is not for them.
_NON_CHAT_PLATFORMS = frozenset({"", "cli", "tui", "desktop", "local", "cron", "webhook", "msgraph_webhook",
                                 "api_server"})


def _managed_chat_session() -> bool:
    """A managed-browser session on a chat platform (Telegram, Discord, ...), not cron or an API turn."""
    try:
        from tools.approval_context import _get_session_platform, _is_cron_approval_context
        from tools.browser_control_route import is_managed

        platform = str(_get_session_platform() or "").strip().lower()
        return is_managed() and platform not in _NON_CHAT_PLATFORMS and not _is_cron_approval_context()
    except Exception:  # health: allow BLE001 -- only picks a refusal text; the refusal itself stands
        return False


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
        if _managed_chat_session():
            # dlz: B20, a chat (Telegram) has no masked prompt; the user keeps logins in 1Password and a
            # managed slot can be handed to him. "headless/cron/API, run hermes vault add" was wrong here.
            return json.dumps({"success": False, "error_type": "prompt_unavailable",
                               "error": (f"This chat cannot take a password securely: save it in 1Password (Hermes "
                                         f"vault) for {origin}, or send the user a browser_control handoff for this "
                                         "tab so they sign in themselves.")})
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


_TAB_PROBES["otp"] = _deep_probe("input[autocomplete=one-time-code], input[name*=otp i], input[name*=code i], "
                                  "input[id*=otp i], input[id*=code i], input[name*=totp i], input[aria-label*=code i]")


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


def _saved_login(handle: str) -> tuple[Any, Any]:
    """(backend, meta) for a vault handle, or (None, None) when nothing is saved under it (or its manager is
    locked): enter_code then asks the user instead of minting a code."""
    from agent.vault_backends import UnlockRequired, backend_for_handle

    backend = backend_for_handle(handle) if handle else None
    if backend is None:
        return None, None
    try:
        return backend, backend.get_meta(handle)
    except UnlockRequired:
        return backend, None


@plugin_errors_as_refusal
def browser_vault_enter_code(
    handle: str = "",
    task_id: Optional[str] = None,
    code: str = "",
    expected_origin: str = "",
) -> str:
    """Second factor: fill the one-time code the CURRENT page asks for. If the saved login (``handle``) has an
    authenticator seed, the code is minted server-side and nobody is asked; otherwise the user is prompted on
    their surface for the code their phone/email/app shows. The code goes into the page over the supervisor
    socket and never enters the conversation. A saved login's code is only minted on its saved origin(s).

    dlz: with ``code`` supplied, that explicit code is used directly — no TOTP lookup, no interactive prompt —
    so an unattended session (e.g. Telegram) can hand over a code obtained through Hermes' own authorized
    mail workflow, or (owner policy 2026-11-09) a one-time code the owner sent in chat. ``expected_origin`` is mandatory in that mode and must equal the current page origin;
    a login ``handle``'s bound origin must also agree. The supplied code is registered with the redaction
    boundary the same way a generated one is."""
    from agent.redact import register_vault_redaction_value
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

    # A saved login mints its code only on its saved origin(s), checked before any code exists. An explicit
    # code is governed by expected_origin and the handle's bound origins instead (_supplied_code_origin_refusal).
    backend, meta = _saved_login(handle) if explicit_code is None else (None, None)
    if meta is not None:
        bound = _bound_page_origin(effective_task_id, meta, "otp")
        if isinstance(bound, dict):
            if bound.get("error_type") == "origin_mismatch":
                bound["next"] = ("If this page asks for the code of a different account, call enter_code without a "
                                 "handle so the user is asked.")
            return json.dumps(bound)
        origin = bound
    else:
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

    def write(code_value: str, source: str) -> str:
        register_vault_redaction_value(code_value, known_to_model=source == "supplied")
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
        filled = _filled_count(parsed, len(fills))
        if filled is None:
            return json.dumps(_FILL_UNVERIFIED)
        return json.dumps({"success": bool(filled), "filled_fields": filled, "origin": origin, "source": source,
                           "next": "Submit the form (many sites auto-submit when the last digit lands)."})

    # A supplied code takes precedence: no TOTP lookup and no prompt. A malformed one already failed
    # above, so there is no silent fallback to another source.
    if explicit_code is not None:
        return write(explicit_code, "supplied")
    if meta is not None:  # a saved login on its bound origin: mint + write inside the backend's fill_context
        written = write_backend_code(backend, handle, meta, origin, lambda value: write(value, backend.name))
        if written is not None:
            return written
    prompt = get_code_prompt_callback()
    if prompt is None or not can_prompt_here():
        return json.dumps({"success": False, "error_type": "prompt_unavailable",
                           "error": (f"{site} asks for a one-time code and this session cannot ask the user (headless/cron/API). "
                                     "Save an authenticator key for this login so codes can be generated automatically, or "
                                     "obtain the code through Hermes' own mail workflow (or ask the user to send the one-time "
                                     "code in chat) and pass it as the code argument with expected_origin set to the page's origin.")})
    typed = (prompt(site, "") or "").strip().replace(" ", "").replace("-", "")
    if not typed:
        return json.dumps({"success": False, "error_type": "code_declined",
                           "error": "The user did not enter a code. Do not ask again this turn."})
    return write(typed, "user")


def _inspect_fill_controls(task_id: str, nonce: str, kind: str, bind: str = "") -> tuple[list, list, Optional[str]]:
    """``(raw_controls, classified, refusal)`` for this inspection's stamps (and, with ``bind``, its identity
    marks for the signup writer); nothing secret is involved yet."""
    from agent.vault_login_classifier import (
        ClassifiedLoginControl, LoginControl, build_inspection_js, classify_checkout_control, classify_login_control,
    )

    inspect = _eval_js(task_id, build_inspection_js(nonce, bind))
    if not inspect.get("success"):
        return [], [], json.dumps(
            {"success": False, "error": f"Could not inspect page inputs: {inspect.get('error', 'eval failed')}"}
        )
    raw_controls = _parse_json_result(inspect.get("result"))
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    if not isinstance(raw_controls, list):
        return [], [], json.dumps({"success": False, "error": "Page input inspection returned no usable controls."})

    classify = classify_login_control if kind == "login" else classify_checkout_control
    classified: list[ClassifiedLoginControl] = []
    for raw in raw_controls:
        if not isinstance(raw, dict):
            continue
        result = classify(LoginControl.from_dict(raw))
        if result is not None:
            classified.append(result)
    return raw_controls, classified, None


def _no_match_hint(kind: str, raw_controls: list, classified: list) -> str:
    """What to do when no control took the saved item, read from the page's controls (never values)."""
    if kind == "login":
        if any("new-password" in str(r.get("autocomplete") or "").lower().split()
               for r in raw_controls if isinstance(r, dict)):
            return ("The page has a new-password field: for creating an account call fill again with "
                    "purpose=signup; for a password change, ask the user.")
        tokens = {getattr(c, "token", "") for c in classified}
        if tokens & {"username", "email", "tel"} and "current-password" not in tokens:
            return ("This step asks only for the identifier: type it yourself with the browser's input tool (it is "
                    "in browser_vault list), continue, then call fill again on the password step.")
    return "Read the form: the right page or step may not be open yet. Do not retry the same fill unchanged."


def _parse_fill_result(fill_result: dict, page_origin: str, refusal_tag: str = "") -> tuple[Any, Optional[str]]:
    """``(parsed, refusal)``: the native write's JSON, or the origin_changed refusal.

    The answer is page-produced: no page string is echoed (a forged ``found`` could carry an encoded value),
    and with ``refusal_tag`` the no-write claim is made only when the answer carries this fill's tag, which
    the script returns solely from its pre-write origin check."""
    parsed = _parse_json_result(fill_result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        if refusal_tag and parsed.get("tag") != refusal_tag:
            return parsed, json.dumps(_FILL_UNVERIFIED)
        return parsed, json.dumps(
            {
                "success": False,
                "error_type": "origin_changed",
                "error": (
                    "Refused: the page navigated away from the bound origin "
                    f"({page_origin}) before the fill could run. Nothing was written."
                ),
            }
        )
    return parsed, None


_FILL_UNVERIFIED = {
    "success": False, "error_type": "fill_result_unverified",
    "error": "The page answered the fill with a result Hermes could not verify; the value may have been written. "
             "Nothing was submitted. Do not submit or retry; check the page and tell the user.",
}


def _filled_count(parsed: Any, most: int) -> Optional[int]:
    """The write script's ``filled`` count, or None when the page-produced answer is not a plain integer in
    ``0..most``: a string or float (a hooked JSON.stringify can return ``{"filled": "<char codes>"}``) is never
    converted or echoed. A dict without ``filled`` counts as 0 (nothing claimed)."""
    if not isinstance(parsed, dict):
        return 0
    filled = parsed.get("filled", 0)
    if type(filled) is not int or not 0 <= filled <= most:  # bool is not a count either
        return None
    return filled


def _register_redactions(values: Any) -> Optional[str]:
    """Register every secret value for output scrubbing BEFORE it touches the page; a refusal if that fails."""
    from agent.redact import register_vault_redaction_value

    try:
        for value in values:
            register_vault_redaction_value(value)
    except Exception:  # health: allow BLE001 -- without redaction the write must not run: fail closed
        return json.dumps({"success": False, "error_type": "redaction_unavailable",
                           "error": "Secret redaction could not be registered. Nothing was written."})
    return None


@plugin_errors_as_refusal
def browser_vault_fill(handle: str, task_id: Optional[str] = None, merchant_origin: str = "",
                       purpose: str = "login", cart: Any = None, approve_origins: Any = None, *,
                       with_identifier: bool = False) -> str:
    """Fill the current page's password field from a vault handle.

    Password-only: the identifier is agent-visible metadata (see
    browser_vault_list) and is typed by the agent via normal input tools.
    The password is resolved server-side and injected via in-page JS over
    the supervisor CDP WebSocket; the result reports only counts/metadata.
    """
    from tools import browser_vault_signup as signup
    from tools.browser_vault_preview import active_page
    from agent.vault_backends import UnlockRequired, backend_for_handle

    effective_task_id = task_id or "default"
    if purpose not in ("login", "signup"):
        return json.dumps({"success": False, "error_type": "invalid_purpose",
                           "error": "Use purpose=login (default) or signup. Nothing was written."})
    if active_page() is not None and (refusal := _preview_refusal()) is not None:
        return refusal  # dlz: the Desktop key button (vault.preview.fill) reaches here without _handle_vault
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
    refusal = signup.kind_refusal(purpose, meta.kind)
    if refusal:
        return refusal
    if meta.kind == "payment" and active_page() is not None:
        # dlz: card fills run only under the managed browser's consent and capture fences.
        return json.dumps({"success": False, "error_type": "payment_destination_requires_managed_browser",
                           "error": "Cards are filled only in the managed browser, never in the in-app browser. "
                                    "Nothing was written."})
    if meta.kind == "payment":
        # dlz: native card fill (tools/browser_vault_payment.py): the supervisor checks the merchant against the
        # card's bound and hosted origins before consent, re-checks before the write, and resolves the card
        # inside the backend's fill_context only after consent.
        from tools.browser_vault_payment import fill_payment

        return fill_payment(backend, meta, effective_task_id, merchant_origin, cart=cart,
                            approve_origins=approve_origins)
    # dlz (payment lockdown): a login/address fill reads and writes the page.
    return _outside_payment_lock(effective_task_id, lambda: _fill_non_card(backend, handle, meta, effective_task_id,
                                                                           purpose, with_identifier))


def _fill_non_card(backend: Any, handle: str, meta: Any, effective_task_id: str, purpose: str,
                   with_identifier: bool = False) -> str:
    # Origin binding pre-check (the authoritative re-check runs inside the fill script), before the
    # backend's fill_context is entered and before any value is resolved.
    page_origin = _bound_page_origin(effective_task_id, meta, meta.kind)
    if isinstance(page_origin, dict):
        return json.dumps(page_origin)

    with fill_context(backend):
        refusal = snapshot_refusal(backend, handle, meta, page_origin)
        if refusal is not None:
            return json.dumps(refusal)
        return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id, purpose, with_identifier)


def _identifier_fill(classified: list, password_fill: list[dict[str, Any]], identifier: str) -> list[dict[str, Any]]:
    """The username/email/phone control for a user-chosen login (the Desktop key button fills both fields;
    the agent types the identifier itself). Prefers a control in the password field's form; on an
    identifier-only step (no password field yet) it is the whole fill."""
    fields = [c for c in classified if c.token in ("username", "email", "tel")]
    if not identifier or not fields:
        return []
    anchor = password_fill[0]["index"] if password_fill else None
    form = next((c.control.form_index for c in classified if c.control.index == anchor), None)
    best = min(fields, key=lambda c: (c.control.form_index != form, -c.score, c.control.index))
    return [{"index": best.control.index, "token": best.token, "value": identifier}]


def _fill_bound_page(backend: Any, handle: str, meta: Any, page_origin: str, effective_task_id: str,
                     purpose: str = "login", with_identifier: bool = False) -> str:
    """Inspect the page, resolve the item's secret and write it; runs inside the backend's fill_context
    after the origin guard passed. ``with_identifier`` also writes the login's (non-secret) identifier
    (login purpose only: a signup write targets the new-password controls alone)."""
    from agent.vault_login_classifier import select_checkout_fills
    from tools import browser_vault_signup as signup
    from agent.vault_backends import BackendError, UnlockRequired
    from agent.vault_store import ADDRESS_FIELDS, PAYMENT_FIELDS, scrub_secret_from_text

    # ── Inspect + classify page controls ────────────────────────────────────
    nonce = secrets.token_hex(8)  # binds this fill to THIS inspection's stamps
    bind = secrets.token_hex(8)  # never in the DOM: names the identity marks the signup writer checks
    raw_controls, classified, refusal = _inspect_fill_controls(effective_task_id, nonce, meta.kind, bind)
    if refusal:
        return refusal
    signup_target, refusal = signup.gate(purpose, raw_controls, handle, meta.label, page_origin,
                                         lambda: _current_page_origin(effective_task_id))
    if refusal:
        return refusal
    if not classified and signup_target is None:
        return json.dumps({"success": False, "error_type": "no_matching_field",
                           "error": f"No {meta.kind} form fields were found on the current page. Nothing was written.",
                           "next": _no_match_hint(meta.kind, raw_controls, classified)})

    # ── Resolve secret and fill (secret never enters any logged string) ─────
    try:
        if meta.kind == "login":
            secret = {"password": backend.resolve_password(handle)}
            fills = signup.password_fills(signup_target, classified, secret["password"])
            if with_identifier and signup_target is None:
                fills += _identifier_fill(classified, fills, str(meta.identifier or ""))
        else:
            secret = backend.resolve_secret(handle)
            fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS if meta.kind == "payment" else ADDRESS_FIELDS)
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault action=unlock."})
    except BackendError:
        raise  # typed manager failures carry advice and no secret; typed_page_op turns them into results
    except Exception:  # health: allow BLE001 -- untyped backend errors can carry secret bytes: refuse without echoing them
        return json.dumps({"success": False, "error_type": "secret_resolution_failed",
                           "error": "Could not resolve the saved item. Nothing was written."})
    if not fills:
        # dlz: B19, a typed refusal with a next step (12 retry loops on identifier-first / signup pages).
        return json.dumps({"success": False, "error_type": "no_matching_field",
                           "error": f"No fillable {meta.kind} field matched the saved item on this page. Nothing "
                                    "was written.", "next": _no_match_hint(meta.kind, raw_controls, classified)})

    # Register the secret bytes with the model-egress redaction boundary
    # BEFORE they touch the page: any later browser_* result (including
    # browser_cdp Runtime.evaluate reads) that echoes them is scrubbed.
    # Address values are not secrets but the card fields are: register every payment value.
    refusal = _register_redactions(secret.values() if meta.kind == "payment" else [secret.get("password", "")])
    if refusal:
        return refusal

    tokens = signup.outcome_tokens() if signup_target is not None else None
    refusal_tag = "hv-" + secrets.token_hex(12)
    try:
        fill_result = _eval_js_secret(
            effective_task_id, signup.fill_expression(fills, page_origin, nonce, signup_target, bind=bind,
                                                      tokens=tokens, refusal_tag=refusal_tag)
        )
    except Exception as exc:  # health: allow BLE001 -- any CDP failure becomes a typed result; its text is scrubbed of the secret
        return signup.uncertain(signup_target, backend.name, meta.kind, page_origin) or json.dumps(
            {"success": False, "error": scrub_secret_from_text(str(exc), secret)}
        )
    if not fill_result.get("success"):
        # dlz: a signup write may have landed before the transport failed: never "nothing written".
        unsure = signup.uncertain(signup_target, backend.name, meta.kind, page_origin)
        if unsure:
            return unsure
        err = scrub_secret_from_text(str(fill_result.get("error") or "fill failed"), secret)
        out = {"success": False, "error": err}
        if fill_result.get("error_type"):
            out["error_type"] = fill_result["error_type"]
        return json.dumps(out)

    if signup_target is not None:  # its answer is one of this fill's outcome tokens, never page JSON
        return signup.finish(fill_result.get("result"), backend.name, meta.kind, page_origin, signup_target, tokens)
    parsed, refusal = _parse_fill_result(fill_result, page_origin, refusal_tag)
    if refusal:
        return refusal
    filled = _filled_count(parsed, len(fills))
    if filled is None:
        return json.dumps(_FILL_UNVERIFIED)

    out = {"success": bool(filled), "filled_fields": filled, "backend": backend.name,
           "kind": meta.kind, "origin": page_origin}
    if meta.kind == "login":
        out["next"] = ("Submit. If the site then asks for a verification code, call browser_vault action=enter_code with this handle"
                       + (" (a code will be generated automatically)." if meta.has_otp else "."))
    if meta.kind != "login":
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return json.dumps(out)


_TAB_PROBES["letter"] = "!!document.querySelector('input[type=password], input[type=text], input:not([type])')"


def _confirm_letter_origin(organization: str, origin: str) -> str:
    """The page is not on a host the letter printed: only the user can say it belongs to the sender.
    Returns the consent outcome (tools.browser_vault_signup.ask_consent)."""
    from tools.browser_vault_signup import ask_consent

    return ask_consent(
        f"Sign in to {origin} with the temporary login from the {organization or 'photographed'} letter",
        "The letter does not print this site's address, so Hermes cannot match it to the sender. The "
        "one-time password never enters the conversation. Approve only if this site belongs to the sender.",
        surface="vault-letter", title="Use letter login on this site?")


def _confirm_letter_reuse(organization: str, origin: str) -> str:
    """Whether a letter's password may be used again is printed by the sender, not knowable here.
    Returns the consent outcome (tools.browser_vault_signup.ask_consent)."""
    from tools.browser_vault_signup import ask_consent

    return ask_consent(
        f"Sign in to {origin} again with the {organization or 'photographed'} letter login",
        "The earlier sign-in worked and its session has expired. Approve only if this letter's password stays "
        "valid for another sign-in (for example, the site says it works until you send the form). Signing in "
        "again does not send, sign or change anything.",
        surface="vault-letter", title="Sign in again with the letter?")


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
    from tools.browser_vault_signup import not_given, outcome_of

    outcome = outcome_of(_confirm_letter_reuse(cred.organization, origin))
    if outcome != "accepted":
        # dlz: B17, a timeout or a missing approval surface is not the user's "no".
        return not_given("reuse_declined", "Reusing this letter's password", outcome)
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
        from tools.browser_vault_signup import not_given, outcome_of

        outcome = outcome_of(_confirm_letter_origin(cred.organization, origin))
        if outcome != "accepted":
            return not_given("origin_declined", "This site for the letter login", outcome)

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
    filled = _filled_count(parsed, len(fills))
    if filled is None:
        return refuse(_FILL_UNVERIFIED["error_type"], _FILL_UNVERIFIED["error"])
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
        "Fill saved logins, 2FA codes, cards and addresses on the CURRENT browser page. Secrets never "
        "enter the conversation. "
        "Actions: list (call first: saved logins/cards/addresses as handles; logins carry the identifier you type "
        "yourself with the browser's input tool; locked password managers appear under `locked`); fill (handle: "
        "the password for a login, card fields after the user confirms, address fields; login/address fills require "
        "their bound origin; a 1Password card can be approved for a selected HTTPS merchant, use merchant_origin to "
        "select its exact tab; card fields may sit in one document or be split across the merchant and ONE hosted "
        "payment provider from the card's hosted_origins, each field exactly once; fields across two providers, "
        "duplicate or ambiguous fields, a missing number/CVC/expiry and unlisted origins are refused; HTTPS is not a "
        "seller reputation check: verify the merchant and checkout terms before requesting consent; it never "
        "submits; purpose=signup separately asks the user to approve account creation with this exact login item "
        "on its exact origin, then fills one unambiguous new-password field on a registration form, plus its ONE "
        "explicitly labelled confirm-password field in the same form when there is one (same password in both); "
        "ambiguous password pairs, a third password, mixed login, OTP and recovery forms are refused; stop on "
        "signup_declined when its consent_outcome is refused; timeout or unavailable is not a refusal: ask the user "
        "to approve once more; it succeeds only when every approved field holds the password after the page "
        "settled: signup_fill_partial, signup_form_changed, signup_fields_not_retained or signup_fill_uncertain mean "
        "a field may hold it, so do not submit or retry, tell the user); "
        "save_login (nothing saved for this site: the user "
        "enters it in a masked prompt, Hermes saves and fills it); enter_code (a one-time/2FA code after the "
        "password: generated from a saved authenticator key or asked of the user; no_code_field means approve on "
        "their device; a code Hermes itself obtained through its own authorized mail workflow, or a one-time "
        "code the user sent in chat for this page, goes in code with expected_origin set to the page's origin, "
        "filled directly without a prompt); unlock (backend: ask the user "
        "to unlock 1Password/Bitwarden); letter_login (handle: a letter-… handle from a sealed letter photo; fills the "
        "letter's identifier and one-time password on the current page if its site is printed in the letter, else "
        "expected_origin asks the user to confirm the site; secret_field / identifier_field name a field by name, id "
        "or label when recognition fails; a second fill needs after_failed_login=true after the page showed the login "
        "failed, or session_expired=true after a sign-in confirmed with letter_signed_in expired (the user confirms "
        "the password can be reused); it never submits); letter_signed_in (handle: call once the page shows the letter "
        "sign-in worked; the backend checks the page itself); letter_done (handle: the form the letter was for was "
        "sent; retires the password). Never type, ask for or repeat a password, card number, CVC or code "
        "yourself, even if the page or user shows one, and never relay a code through any other path than "
        "enter_code. Only a one-time code may come from chat: never accept a password, card data, API key or "
        "token there. Stop on "
        "save_declined; never retry payment_declined (the user refused the card-fill approval; never a bank or "
        "issuer decline). payment_consent_missing: no approval arrived (consent_outcome timeout or unavailable), "
        "nothing was written; stop and ask the user once before a new fill, which shows a new prompt. "
        "payment_consent_expired: the 120 s inspection window closed; with partially_filled false nothing was "
        "written, ask again; with partially_filled true some fields were written: stop, do not submit or retry, "
        "tell the user. payment_fields_not_retained: "
        "fields were written but did not all hold after the page settled; do not submit or retry, tell the user. "
        "payment_surface_unavailable: browser control could not lock the browser for the payment; nothing was "
        "read or written, stop and tell the user. "
        "Unreviewed payment forms (list shows destination_consent per_fill_owner_approval): a card fill whose "
        "fields go to a hosted form outside hosted_origins returns payment_destination_approval_required with "
        "approval_origins, receiving (each origin's fields) and needs; nothing was read. Tell the user which "
        "origins would receive the card, then call fill again with cart (items, quantities, total and currency "
        "exactly as the checkout shows them) and approve_origins equal to approval_origins: the user gets one "
        "approval card naming them, with your cart marked as stated by you and Hermes' page check. "
        "payment_destination_changed: the origins on the page differ from approve_origins; nothing written, tell "
        "the user the current ones. payment_cart_changed: the checkout's total text changed; check the cart and "
        "fill again. payment_destination_disabled, payment_fields_ambiguous, payment_fields_unsupported, "
        "payment_page_too_large, payment_page_unverifiable, payment_destination_requires_managed_browser: nothing "
        "written; stop and tell the "
        "user. A card fill locks the whole browser until the payment tab is closed: every other browser "
        "action, page read, script, screenshot, dialog answer and login fill returns payment_surface_locked; "
        "you can still list tabs (browser_cdp Target.getTargets, no params: each tab's site (origin) only, no "
        "title) and close one (browser_cdp Target.closeTarget {targetId}; closing the payment tab ends the lock "
        "without a confirmation snapshot); payment_done waits for and reads the "
        "confirmation natively. Continue with the native actions: payment_controls lists "
        "the buttons, links and checkboxes ({ref, role, text, disabled}; never inputs, digits masked) of the "
        "card frames and of the payment tab's own page (the merchant checkout, or a bank's 3-D Secure page it "
        "was redirected to) unless that page is a card form; payment_click (ref) makes one trusted click on a "
        "listed control, so the Pay button is clicked there; refs die when the page changes, when you list again, and after one press (successful or uncertain): "
        "a second press needs a new payment_controls. "
        "The user's chat "
        "go for the purchase covers pressing Pay once; never click Pay without it, never click it again when "
        "payment_click_uncertain says a press may have landed. Page dialogs while locked: alerts are accepted and "
        "prompts dismissed by Hermes; a confirm waits and payment_controls / payment_click / payment_done report "
        "dialog_pending {id, type, origin, on_payment_tab_top, on_shop_page, text} for the payment tab (text "
        "redacted, bounded, every 3+ digit run and date masked except an amount exactly equal to the cart total "
        "the user approved; dialogs on other tabs only as "
        "other_dialogs_pending: n; payment_done returns at once with payment_dialog_pending). payment_dialog "
        "(dialog_id, accept true or false) answers that confirm once, never retried: only a confirm on the "
        "payment tab's own top page (on_payment_tab_top true), and accept only on the shop's own page "
        "(on_shop_page true; a bank or provider page's confirm can only be dismissed); card frames, other tabs "
        "and unknown sources are refused. Accept only a confirm on the shop page that matches the Pay you were "
        "told to make (e.g. 'Place order for <amount>?'); for anything else (upsells, subscriptions, unknown) "
        "ask the user. An unanswered confirm is "
        "dismissed after the dialog timeout. On 3-D Secure or redirect pages, normally just "
        "wait and call payment_done; click a bank page only when it asks for a button and the user agreed. "
        "After pressing Pay, call payment_done once; it is what ends the lock: it waits up to 120 s (through 3-D Secure and payment-provider pages) for the order "
        "confirmation on the checkout's origin (expected_origin only when the checkout ran on a payment provider's own "
        "page), then "
        "returns its url, title, visible text and http(s) links (never field values or screenshots), opens it in a "
        "new tab (new_tab) and closes the payment tab; lock_released true means browser control confirmed the "
        "unlock, false (with note) means it did not, so follow the note. payment_confirmation_not_reached: no "
        "confirmation within the wait (url: the tab's site only), the lock stays, nothing was clicked or closed; never press Pay again; "
        "follow the error (call payment_done again after the user finishes a bank check, or ask the user). "
        "fill_result_unverified: the page's answer to a fill could not be verified and a field may hold the "
        "password; do not submit or retry, tell the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "fill", "save_login", "enter_code", "unlock", "letter_login",
                                                  "letter_signed_in", "letter_done", "payment_controls",
                                                  "payment_click", "payment_done", "payment_dialog"]},
            "ref": {"type": "string", "description": "payment_click only: a ref from payment_controls."},
            "accept": {"type": "boolean", "description": "payment_dialog only: true accepts, false dismisses the "
                                                         "dialog_pending confirm."},
            "dialog_id": {"type": "string", "description": "payment_dialog only: dialog_pending.id of the confirm "
                                                           "to answer."},
            "handle": {"type": "string", "description": "fill / enter_code / letter_*: a handle from list or a letter note."},
            "purpose": {"type": "string", "enum": ["login", "signup"], "default": "login",
                        "description": "fill only: login preserves current-password behavior; signup requires fresh "
                                       "native approval to create an account with this exact login item and origin. "
                                       "Fills one explicit new-password input, or that input plus one explicitly "
                                       "labelled confirm-password input in the same form; never submits."},
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
            "cart": {"type": "object", "description":
                     "fill, card only: the purchase exactly as the checkout shows it, shown to the user as stated by "
                     "you. Required after payment_destination_approval_required; optional otherwise.",
                     "properties": {
                         "items": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                             "type": "object",
                             "properties": {"description": {"type": "string", "maxLength": 80},
                                            "quantity": {"type": "integer", "minimum": 1, "maximum": 99}},
                             "required": ["description", "quantity"]}},
                         "total": {"type": "string", "description": "e.g. \"22.66\" (two decimals, no symbol)."},
                         "currency": {"type": "string", "description": "ISO code, e.g. \"EUR\"."}},
                     "required": ["items", "total", "currency"]},
            "approve_origins": {"type": "array", "items": {"type": "string"}, "description":
                                "fill, card only: exactly the approval_origins of payment_destination_approval_required."},
            "code": {"type": "string", "maxLength": 16,
                     "description": "enter_code only: a one-time code Hermes obtained through its own authorized workflow (e.g. read "
                                    "from the user's mail) or a one-time code the user sent in chat for this page (SMS/2FA/"
                                    "verification code), exactly as issued including hyphens and leading zeros. Requires expected_origin. "
                                    "Never put a password or other long-lived secret here."},
            "expected_origin": {"type": "string",
                                "description": "Mandatory with code: the page's origin (scheme://host[:port]). The fill is refused unless it matches exactly. "
                                               "payment_done only: the shop's HTTPS origin, needed (and honoured) only when the checkout ran on a payment provider's own page; otherwise ignored (the result says so)."},
            "backend": {"type": "string", "enum": ["onepassword", "bitwarden"], "description": "unlock only."},
            "label": {"type": "string", "description": "save_login only: short site name (default: the host)."},
            "target": {"type": "string", "enum": ["browser", "preview"],
                       "description": "fill / save_login / enter_code: 'preview' = the Desktop in-app browser "
                                      "page beside the chat, 'browser' = the managed automation browser."},
        },
        "required": ["action"],
    },
}


def _bot_desktop_browser_session(task_id: Optional[str]) -> bool:
    from tools.browser_session import session_for_task, shares_bot_desktop_browser
    return shares_bot_desktop_browser(session_for_task(task_id))


def typed_page_op(fn) -> str:
    """dlz: ``fn()`` with the vault's typed failures as results: ``_PageUnavailable`` and a typed manager
    failure (``BackendError``: rate budget, expired token, network, missing item; model-facing advice and
    no secret) come back as their JSON result. The preview paths (``target=preview``, the Desktop key
    button) skip ``_fenced_page_op`` and use this directly."""
    from agent.vault_backends import BackendError
    try:
        return fn()
    except (_PageUnavailable, BackendError) as exc:
        return json.dumps(exc.result())


def _fenced_page_op(task_id: Optional[str], fn) -> str:
    """Vault operations focus, inspect and fill the page over the supervisor socket, bypassing
    browser_exec; they must honour the Bot Desktop lease like every other page access, or a human
    typing a credential on the taken-over screen could be read or written to."""
    from tools.browser_session import run_fenced, session_for_task

    def op() -> dict[str, Any]:
        try:
            if _is_managed():
                # dlz: control must confirm the agent still holds the slot lease before any page read.
                _managed_supervisor(task_id or "default", admit=True)
        except _PageUnavailable as exc:
            return {"raw": json.dumps(exc.result())}
        return {"raw": typed_page_op(fn)}

    res = run_fenced(session_for_task(task_id), op)
    return res["raw"] if "raw" in res else json.dumps(res)


def _payment_lock_refusal(task_id: Optional[str]) -> Optional[str]:
    """dlz (payment lockdown): ``payment_surface_locked`` for a non-card page operation while a card payment
    locks the task's slot. Only the native payment actions (card fill, payment_controls / payment_click,
    payment_done) work on a locked slot; a slot this process never attached a supervisor to holds no claim."""
    from tools.browser_payment_surface import locked_refusal
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    supervisor = SUPERVISOR_REGISTRY.get(task_id or "default")
    refused = locked_refusal(supervisor) if supervisor is not None else None
    return json.dumps(refused) if refused else None


# dlz (payment lockdown): the fence of the non-card page operation running in this context, if any.
_VAULT_FENCE: contextvars.ContextVar = contextvars.ContextVar("vault_payment_fence", default=None)


def _outside_payment_lock(task_id: Optional[str], op) -> str:
    """dlz (payment lockdown): run a non-card page operation only while no card payment locks the slot. Every
    native read or write it dispatches (``_native``) is fenced again right before it goes out, so a claim
    during a user prompt (enter_code waits for the code) stops the next write; the whole payload (success or
    error) is withheld when a claim happened while the operation ran. Human waits never hold the claim lock."""
    from tools import browser_payment_surface as pay
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    refused = _payment_lock_refusal(task_id)
    if refused:
        return refused
    supervisor = SUPERVISOR_REGISTRY.get(task_id or "default")
    fence = {"sup": supervisor, "before": pay.epoch(supervisor) if supervisor is not None else 0, "refused": None}
    token = _VAULT_FENCE.set(fence)
    try:
        out = op()
    finally:
        _VAULT_FENCE.reset(token)
    if fence["refused"] is not None:
        return json.dumps(fence["refused"])
    if fence["sup"] is not None and pay.epoch(fence["sup"]) != fence["before"]:
        return json.dumps(pay.locked_withheld())
    return out


def _fence_refused() -> bool:
    fence = _VAULT_FENCE.get()
    return fence is not None and fence["refused"] is not None


def _native(supervisor: Any, call) -> dict:
    """One native dispatch (evaluate, focus) on the supervisor socket. Inside a fenced non-card operation it
    goes out only under the claim lock, with the slot unlocked and no claim since the operation started (a
    different supervisor than the one first seen refuses too); a refusal dispatches nothing."""
    fence = _VAULT_FENCE.get()
    if fence is None:
        return call()
    from tools import browser_payment_surface as pay

    if fence["refused"] is not None:
        return {"ok": False, "error": pay.LOCKED}
    if fence["sup"] is None:
        fence["sup"], fence["before"] = supervisor, pay.epoch(supervisor)
    if fence["sup"] is not supervisor:
        fence["refused"] = pay.locked_withheld()
        return {"ok": False, "error": pay.LOCKED}
    state, out = pay.fenced_dispatch(supervisor, fence["before"], call)
    if state != "ok":
        fence["refused"] = out
        return {"ok": False, "error": pay.LOCKED}
    return out


def _payment_surface_action(task_id: Optional[str], ref: Optional[str]) -> str:
    """dlz: native actions on a locked payment tab (managed browser only)."""
    from tools import browser_payment_surface as surface

    if not _is_managed():
        return json.dumps({"success": False, "error_type": "payment_surface_none",
                           "error": "Only the managed browser protects card frames; use browser_exec as usual."})
    supervisor = _managed_supervisor(task_id or "default")
    if ref is None:
        return json.dumps(surface.payment_controls(supervisor))
    return json.dumps(surface.payment_click(supervisor, ref))


def _payment_dialog_action(task_id: Optional[str], dialog_id: Any, accept: Any) -> str:
    """dlz (lockdown, owner decision 2026-11-11): one native answer to a confirm the payment lock holds."""
    from tools.browser_payment_done import payment_dialog

    if not _is_managed():
        return json.dumps({"success": False, "error_type": "payment_surface_none",
                           "error": "Only the managed browser locks a card payment; use the browser tools as usual."})
    return json.dumps(payment_dialog(_managed_supervisor(task_id or "default"), dialog_id, accept))


def _payment_done_action(task_id: Optional[str], expected_origin: str) -> str:
    """dlz (lockdown §2): the native confirmation handover after Pay (managed browser only)."""
    from tools.browser_payment_done import payment_done

    if not _is_managed():
        return json.dumps({"success": False, "error_type": "payment_surface_none",
                           "error": "Only the managed browser locks a card payment; use the browser tools as usual."})
    return json.dumps(payment_done(_managed_supervisor(task_id or "default"), expected_origin))


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
                                           merchant_origin=str(args.get("merchant_origin") or ""),
                                           purpose=str(args.get("purpose") or "login"),
                                           # dlz (Track B): card fills only; passed only when given.
                                           **{k: args[k] for k in ("cart", "approve_origins") if k in args}),
        "save_login": lambda: _outside_payment_lock(tid, lambda: browser_vault_save_login(label=label, task_id=tid)),
        "enter_code": lambda: _outside_payment_lock(tid, lambda: browser_vault_enter_code(
            handle=handle, task_id=tid, code=str(args.get("code") or ""),
            expected_origin=str(args.get("expected_origin") or ""))),
        "letter_login": lambda: _outside_payment_lock(tid, lambda: browser_vault_letter_login(
            handle, task_id=tid, secret_field=str(args.get("secret_field") or ""),
            identifier_field=str(args.get("identifier_field") or ""),
            expected_origin=str(args.get("expected_origin") or ""),
            after_failed_login=args.get("after_failed_login") is True,
            session_expired=args.get("session_expired") is True)),
        "letter_signed_in": lambda: _outside_payment_lock(
            tid, lambda: browser_vault_letter_signed_in(handle, task_id=tid)),
        "payment_controls": lambda: _payment_surface_action(tid, None),
        "payment_click": lambda: _payment_surface_action(tid, str(args.get("ref") or "")),
        "payment_done": lambda: _payment_done_action(tid, str(args.get("expected_origin") or "")),
        "payment_dialog": lambda: _payment_dialog_action(tid, args.get("dialog_id"), args.get("accept")),
    }
    if action not in page_ops:
        return json.dumps({"success": False, "error": f"Unknown action {action!r}; use list, fill, save_login, "
                                                       "enter_code, unlock, letter_login, letter_signed_in, "
                                                       "letter_done, payment_controls, payment_click, payment_done or "
                                                       "payment_dialog."})
    target = args.get("target") or "browser"
    if target == "preview":
        if action not in _PREVIEW_ACTIONS:
            return json.dumps({"success": False, "error_type": "invalid_target",
                               "error": f"target=preview supports fill, save_login and enter_code, not {action}. "
                                        "Nothing was read or written."})
        if (refusal := _preview_refusal()) is not None:
            return refusal
        # The in-app browser is not the Bot Desktop browser; its window owns the page and refuses a
        # changed tab or document itself. Never retried on another browser.
        from tools.browser_vault_preview import run_preview_vault
        return run_preview_vault(kwargs.get("preview_callback"), lambda: typed_page_op(page_ops[action]))
    if target != "browser":
        return json.dumps({"success": False, "error_type": "invalid_target",
                           "error": "target must be 'browser' or 'preview'."})
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
