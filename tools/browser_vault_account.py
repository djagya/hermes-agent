"""``browser_vault`` actions that write the owner's password manager, so a login does not need him (J-0643).

- ``reset_password`` (D-1086 a): on the choose-a-new-password page of a site's emailed reset link, generate a
  password, save it in 1Password FIRST (an existing login item, or a new one in the Hermes vault), then fill the
  page's new-password field (and its one confirmation) with the ``purpose=reset`` writer. A save that fails fills
  nothing, so the site never gets a password nobody holds; a fill that fails after the save says so.
- ``enroll_totp`` (D-1085 a): on a site's authenticator-setup page, read the key the page shows (an otpauth://
  link or the "enter this key" text), save it in the login item's one-time-password field FIRST, then enter the
  code 1Password mints from the stored key.

Non-bank accounts only: both refuse on the ``sensitive`` browser slots, which is where bank-like accounts live
(browser-slots skill; nothing in the vault marks an item as a bank). No owner approval is asked: D-1085 a and
D-1086 a are his standing go for non-bank accounts. Neither the password nor the key is ever returned, logged or
put in an error; both are registered with the redaction boundary before anything else sees them.
"""

from __future__ import annotations

import base64
import json
import secrets
import string
from typing import Any, Optional
from urllib.parse import urlsplit

from tools import browser_vault_signup as signup

_ALPHABET = string.ascii_letters + string.digits + "-_.!@#%+="
_PASSWORD_LENGTH = 20


def _refuse(error_type: str, error: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": error, **extra})


def _bank_refusal(task_id: str) -> Optional[str]:
    from tools.browser_use_cli import vault_route

    if str(vault_route(task_id).get("slot_id") or "").startswith("sensitive"):
        return _refuse("bank_account_refused", "This tab is on a sensitive (bank-like) browser slot: passwords and "
                       "authenticators there stay with the user. Nothing was read, saved or written.")
    return None


def _writable_backend(handle: str) -> tuple[Any, Optional[str]]:
    """The password manager that takes the write: the handle's own, else the first enabled one that can write."""
    from agent.vault_backends import backend_for_handle, enabled_backends
    from tools import browser_vault_tool as vault

    backend = backend_for_handle(handle) if handle else next(
        (b for b in enabled_backends() if callable(getattr(b, "save_login", None))), None)
    if backend is None or not callable(getattr(backend, "save_login", None)):
        return None, _refuse("vault_write_unavailable", "Only a 1Password login can be written by Hermes, and none is "
                             "set up for this handle. Nothing was saved or written.")
    if backend.needs_unlock and not backend.is_unlocked():
        unlocked = json.loads(vault.browser_vault_unlock(backend.name))
        if not unlocked.get("success"):
            return None, json.dumps(unlocked)
    return backend, None


def _login_meta(backend: Any, handle: str) -> tuple[Any, Optional[str]]:
    from tools import browser_vault_tool as vault

    meta = vault._meta_relisted(backend, handle)
    if meta is None or meta.kind != "login":
        return None, _refuse("vault_item_not_found", f"No login item with handle {handle!r}. Call browser_vault "
                             "action=list. Nothing was saved or written.")
    return meta, None


def new_password(max_length: Optional[int]) -> str:
    """A random password of 20 characters (fewer when the field allows fewer, never under 8) with a lower- and
    upper-case letter, a digit and a symbol."""
    length = max(8, min(_PASSWORD_LENGTH, max_length or _PASSWORD_LENGTH))
    while True:
        value = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if all(any(c in group for c in value) for group in
               (string.ascii_lowercase, string.ascii_uppercase, string.digits, _ALPHABET[62:])):
            return value


def reset_password(handle: str, task_id: Optional[str], *, identifier: str = "", label: str = "") -> str:
    from tools import browser_vault_tool as vault

    tid = task_id or "default"
    if (refusal := _bank_refusal(tid)) is not None:
        return refusal
    if not handle and not identifier.strip():
        return _refuse("identifier_required", "Without a handle, pass identifier (the account's email or username) "
                       "so the new 1Password item can sign in later. Nothing was saved or written.")
    backend, refusal = _writable_backend(handle)
    if refusal:
        return refusal
    if handle:
        meta, refusal = _login_meta(backend, handle)
        if refusal:
            return refusal
        origin = vault._bound_page_origin(tid, meta, "login")
        if isinstance(origin, dict):
            if origin.get("error_type") == "origin_mismatch":
                origin["next"] = "Call reset_password without a handle to save a new item for this site."
            return json.dumps(origin)
    else:
        vault._focus_bound_origin(tid, "", "login")
        origin = vault._current_page_origin(tid)
        if not origin:
            return _refuse("no_page", "Open the site's choose-a-new-password page first. Nothing was saved or written.")
    nonce, bind = secrets.token_hex(8), secrets.token_hex(8)
    raw_controls, classified, refusal = vault._inspect_fill_controls(tid, nonce, "login", bind)
    if refusal:
        return refusal
    target, refusal = signup.targets("reset", raw_controls)
    if refusal:
        return refusal
    password = new_password(target.primary.max_length)
    if (refusal := vault._register_redactions([password])) is not None:
        return refusal
    saved, refusal = _save_password(backend, handle, origin, password, identifier.strip(), label.strip())
    if refusal:
        return refusal
    done = {"saved": True, "handle": saved, "created": not handle}
    if vault._current_page_origin(tid) != origin:
        return _refuse("origin_changed", "The page left the reset page before the fill, so nothing was written "
                       "there. The new password is saved in 1Password under handle.", **done,
                       next="Open the reset link again, then call reset_password with this handle.")
    fills = signup.password_fills(target, classified, password)
    tokens = signup.outcome_tokens()
    try:
        answer = vault._eval_js_secret(tid, signup.fill_expression(fills, origin, nonce, target, bind=bind,
                                                                   tokens=tokens, purpose="reset"))
    except Exception:  # health: allow BLE001 -- a CDP failure may have written a field: report it as uncertain
        answer = {"success": False}
    del password, fills
    if not answer.get("success"):
        out = json.loads(signup.uncertain(target, backend.name, "login", origin, "reset"))
    else:
        out = json.loads(signup.finish(answer.get("result"), backend.name, "login", origin, target, tokens, "reset"))
    out.update(done)
    if out.get("success"):
        out["next"] = ("Submit the form; the site then uses the new password, which is saved in 1Password under "
                       "handle. Sign in with fill and this handle.")
    else:
        out["next"] = ("The new password is saved in 1Password under handle, but the page may not hold it. Do not "
                       "submit. Read the page, then call reset_password again with this handle (it saves a fresh "
                       "password in the same item and fills it).")
    return json.dumps(out)


def _save_password(backend: Any, handle: str, origin: str, password: str, identifier: str,
                   label: str) -> tuple[str, Optional[str]]:
    """``(handle, None)`` once the password is in the manager, else ``("", refusal)``: nothing is filled then."""
    from agent.vault_backends import BackendError, UnlockRequired

    not_filled = "Nothing was filled into the page, so the site's password is unchanged."
    try:
        return backend.save_login(handle, origin=origin, password=password, username=identifier, title=label), None
    except BackendError as exc:
        return "", _refuse(exc.error_type, f"{exc.message()} {not_filled}", saved=False)
    except UnlockRequired:
        return "", _refuse("unlock_required", f"{backend.display_name} locked; call browser_vault action=unlock. "
                           + not_filled, saved=False)
    except Exception:  # health: allow BLE001 -- untyped errors may quote the item: fixed text only
        return "", _refuse("vault_save_failed", f"Saving the new password in {backend.display_name} failed. "
                           + not_filled, saved=False)


# Reads the authenticator key a setup page shows: every otpauth:// URI in an attribute (a link, the QR image's
# alt or data) or in an element's own text, and every element whose whole text (or a readonly input's value) is
# a base32 key: 16+ characters of A-Z / 2-7, one case, grouped by spaces or dashes or holding a digit. Read only,
# by the non-secret eval; Python keeps no value it does not store.
_KEY_JS = r"""(() => {
  const uris = [], keys = [];
  const add = (list, v) => { if (v && list.length < 10 && !list.includes(v)) list.push(v); };
  const roots = [];
  (function walk(root) {
    roots.push(root);
    for (const e of root.querySelectorAll("*")) {
      if (e.shadowRoot) walk(e.shadowRoot);
      try { if (e.tagName === "IFRAME" && e.contentDocument && e.contentDocument.location.origin === location.origin) walk(e.contentDocument); } catch (_) {}
    }
  })(document);
  const uriIn = (v) => { const at = v.indexOf("otpauth://"); if (at >= 0) add(uris, v.slice(at).split(/[\s"'<>]/)[0]); };
  for (const root of roots) {
    for (const e of root.querySelectorAll("*")) {
      for (const a of e.getAttributeNames ? e.getAttributeNames() : []) uriIn(e.getAttribute(a) || "");
      if (e.children.length && e.tagName !== "INPUT") continue;
      const t = (e.tagName === "INPUT" ? (e.readOnly ? e.value : "") : (e.textContent || "")).trim();
      uriIn(t);
      const k = t.replace(/[\s-]/g, "");
      if (t.length <= 160 && /^[A-Za-z2-7]{16,128}=*$/.test(k) && (t === t.toUpperCase() || t === t.toLowerCase()) &&
          (/[\s-]/.test(t) || /[2-7]/.test(k))) add(keys, t);
    }
  }
  return JSON.stringify({ uris, keys });
})()"""


def page_key(found: Any) -> tuple[str, list[str], str]:
    """``(value to store, every page form of it, "")`` or ``("", [], reason)``. A TOTP otpauth:// URI wins (it
    keeps the digits, period and algorithm); else the one base32 key the page shows as text."""
    from agent.vault_backends.onepassword import totp_key

    found = found if isinstance(found, dict) else {}
    uris = [u for u in found.get("uris") or [] if isinstance(u, str) and urlsplit(u).netloc.lower() == "totp"
            and _valid_key(totp_key(u))]
    texts = [k for k in found.get("keys") or [] if isinstance(k, str) and _valid_key(totp_key(k))]
    pool = uris or texts
    if not pool:
        return "", [], "no_key_on_page"
    if len({totp_key(v) for v in pool}) > 1:
        return "", [], "several_keys_on_page"
    key = totp_key(pool[0])
    return pool[0], [*uris, *texts, key], ""


def _valid_key(key: str) -> bool:
    if not 16 <= len(key) <= 128:
        return False
    try:
        return len(base64.b32decode(key + "=" * (-len(key) % 8))) >= 10
    except ValueError:
        return False


_KEY_REASONS = {
    "no_key_on_page": ("No authenticator key could be read on this page. Click the page's \"can't scan\" / \"enter "
                       "key manually\" link so the key shows as text, then call enroll_totp again."),
    "several_keys_on_page": "The page shows more than one authenticator key, so none was used.",
}


def enroll_totp(handle: str, task_id: Optional[str]) -> str:
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends import BackendError, UnlockRequired
    from tools import browser_vault_tool as vault

    tid = task_id or "default"
    if (refusal := _bank_refusal(tid)) is not None:
        return refusal
    if not handle:
        return _refuse("handle_required", "enroll_totp needs the login's handle (browser_vault action=list). "
                       "Nothing was saved or written.")
    backend, refusal = _writable_backend(handle)
    if refusal:
        return refusal
    meta, refusal = _login_meta(backend, handle)
    if refusal:
        return refusal
    origin = vault._bound_page_origin(tid, meta, "otp")
    if isinstance(origin, dict):
        return json.dumps(origin)
    read = vault._eval_js(tid, _KEY_JS)
    found = vault._parse_json_result(read.get("result")) if read.get("success") else None
    if isinstance(found, str):
        found = vault._parse_json_result(found)
    key, forms, reason = page_key(found)
    del found
    if not key:
        return _refuse("totp_key_not_found", _KEY_REASONS[reason] + " Nothing was saved or entered.", reason=reason)
    try:
        for form in forms:
            register_vault_redaction_value(form)
    except Exception:  # health: allow BLE001 -- without redaction the key must not travel further: fail closed
        return _refuse("redaction_unavailable", "Secret redaction could not be registered. Nothing was saved.")
    not_entered = "No code was entered, so the site's authenticator is not switched on."
    try:
        added = backend.add_totp(handle, key)
    except BackendError as exc:
        return _refuse(exc.error_type, f"{exc.message()} {not_entered}", saved=False)
    except UnlockRequired:
        return _refuse("unlock_required", f"{backend.display_name} locked; call browser_vault action=unlock. "
                       + not_entered, saved=False)
    except Exception:  # health: allow BLE001 -- untyped errors may quote the item: fixed text only
        return _refuse("vault_save_failed", f"Saving the key in {backend.display_name} failed. " + not_entered,
                       saved=False)
    finally:
        del key, forms
    done = {"saved": True, "handle": handle, "key": "stored" if added else "already_stored"}
    try:
        code = backend.resolve_otp(handle)
    except BackendError as exc:
        return _refuse(exc.error_type, exc.message(), **done)
    if not code:
        return _refuse("totp_code_unavailable", "The key is saved, but 1Password minted no code from it. Do not "
                       "confirm the setup; tell the user.", **done)
    entered = json.loads(vault.browser_vault_enter_code(task_id=tid, code=code, expected_origin=origin))
    entered.pop("source", None)
    entered.update(done)
    entered["next"] = ("Confirm the setup on the page (submit); from then on enter_code with this handle generates "
                       "the codes." if entered.get("success") else
                       "The key is saved in 1Password. Read the page, then call enter_code with this handle once the "
                       "code field is ready, and confirm the setup.")
    return json.dumps(entered)
