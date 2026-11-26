"""``browser_vault`` actions that write the owner's password manager, so a login does not need him (J-0643).

- ``reset_password`` (D-1086 a): on the choose-a-new-password page of a site's emailed reset link, have 1Password
  generate a password in an existing login item (or a new one in the Hermes vault) FIRST, read it back, then fill
  the page's new-password field (and its one confirmation) with the ``purpose=reset`` writer. A save that fails
  fills nothing, so the site never gets a password nobody holds; a fill that fails after the save says so, and a
  retry with the returned handle fills the SAME saved password (a new one only on ``rotate``).
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
from typing import Any, Optional
from urllib.parse import urlsplit

from agent.vault_login_classifier import reset_page_refusal
from tools import browser_vault_signup as signup
from tools.browser_vault_outcome import on_bank_slot

_PASSWORD_LENGTH = 20
# (handle, origin) of passwords 1Password generated and saved that the page has not confirmed holding: a retry
# fills the saved one instead of generating another (the site may already have taken the first). In memory, bounded.
# The value: whether that password has symbols (symbols=false then makes a new one only when it does).
_PENDING: dict[tuple[str, str], bool] = {}


def _refuse(error_type: str, error: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": error, **extra})


def _bank_refusal(task_id: str) -> Optional[str]:
    if on_bank_slot(task_id):  # a sensitive* slot, or one whose id is unknown: fail closed
        return _refuse("bank_account_refused", "This tab is on a sensitive (bank-like) browser slot, or on one this "
                       "tool cannot identify: passwords and authenticators there stay with the user. Nothing was "
                       "read, saved or written.")
    return None


def _writable_backend(handle: str) -> tuple[Any, Optional[str]]:
    """The password manager that takes the write: the handle's own, else the first enabled one that can write."""
    from agent.vault_backends import backend_for_handle, enabled_backends
    from tools import browser_vault_tool as vault

    backend = backend_for_handle(handle) if handle else next(
        (b for b in enabled_backends() if callable(getattr(b, "generate_login", None))), None)
    if backend is None or not callable(getattr(backend, "generate_login", None)):
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


_RESET_REFUSALS = {  # reset_page_refusal's reason: (error_type, text)
    "registration_page": ("registration_page", "This is a registration (sign-up) page, not a password reset. A sign-up "
                          "password is filled with fill purpose=signup, which asks the user."),
    "login_page": ("reset_target_unsupported", "This is a sign-in page, not a choose-a-new-password page. Open the "
                   "emailed reset link."),
    "form_text_unreadable": ("reset_target_unsupported", "This page's form could not be read whole (too long or "
                             "unreadable), so it cannot be told apart from a sign-up form. A sign-up form takes fill "
                             "purpose=signup (it asks the user); otherwise ask the user once."),
    "no_reset_evidence": ("reset_target_unsupported", "Nothing on this page (its address, the field's label or the "
                          "form's text) says it chooses a new password. Open the emailed reset link."),
}


def _reset_target(tid: str, tail: str) -> tuple[Optional[tuple], Optional[str]]:
    """``((nonce, bind, target, classified), None)`` for a fresh inspection of the page when it is a
    choose-a-new-password page this tool may write, else ``(None, refusal)`` ending in ``tail``."""
    from tools import browser_vault_tool as vault

    nonce, bind = secrets.token_hex(8), secrets.token_hex(8)
    raw_controls, classified, refusal = vault._inspect_fill_controls(tid, nonce, "login", bind)
    if refusal:
        return None, refusal
    target, refusal = signup.targets("reset", raw_controls)
    if refusal:
        return None, refusal
    if why := reset_page_refusal(target):
        error_type, text = _RESET_REFUSALS[why]
        return None, _refuse(error_type, text + tail, reason=why)
    return (nonce, bind, target, classified), None


def _reset_page(handle: str, tid: str, identifier: str) -> tuple[Optional[tuple], Optional[str]]:
    """``((backend, origin, target), None)`` once the page is a choose-a-new-password page this call may write,
    else ``(None, refusal)``. Nothing is saved or written yet."""
    from tools import browser_vault_tool as vault

    if (refusal := _bank_refusal(tid)) is not None:
        return None, refusal
    if not handle and not identifier:
        return None, _refuse("identifier_required", "Without a handle, pass identifier (the account's email or "
                             "username) so the new 1Password item can sign in later. Nothing was saved or written.")
    backend, refusal = _writable_backend(handle)
    if refusal:
        return None, refusal
    if handle:
        meta, refusal = _login_meta(backend, handle)
        if refusal:
            return None, refusal
        origin = vault._bound_page_origin(tid, meta, "login")
        if isinstance(origin, dict):
            if origin.get("error_type") == "origin_mismatch":
                origin["next"] = "Call reset_password without a handle to save a new item for this site."
            return None, json.dumps(origin)
    else:
        vault._focus_bound_origin(tid, "", "login")
        origin = vault._current_page_origin(tid)
        if not origin:
            return None, _refuse("no_page", "Open the site's choose-a-new-password page first. Nothing was saved "
                                 "or written.")
    fresh, refusal = _reset_target(tid, " Nothing was saved or written.")
    if refusal:
        return None, refusal
    return (backend, origin, fresh[2]), None


def reset_password(handle: str, task_id: Optional[str], *, identifier: str = "", label: str = "",
                   rotate: bool = False, symbols: bool = True) -> str:
    tid = task_id or "default"
    page, refusal = _reset_page(handle, tid, identifier.strip())
    if refusal:
        return refusal
    backend, origin, target = page
    key = (handle, origin)
    if handle and not rotate and key in _PENDING and (symbols or not _PENDING[key]):  # else the site refused symbols
        saved = handle  # an earlier call saved a password the page may not hold: fill that one again
    else:
        saved, refusal = _generate(backend, handle, origin, identifier.strip(), label.strip(),
                                   max(8, min(_PASSWORD_LENGTH, target.primary.max_length or _PASSWORD_LENGTH)),
                                   symbols)
        if refusal:
            return refusal
        _PENDING.pop(key, None)
        _PENDING[(saved, origin)] = symbols
        while len(_PENDING) > 32:
            _PENDING.pop(next(iter(_PENDING)))
    out = _fill_saved(backend, saved, origin, tid, created=not handle)
    if json.loads(out).get("success"):
        _PENDING.pop((saved, origin), None)
    return out


def _fill_saved(backend: Any, saved: str, origin: str, tid: str, *, created: bool) -> str:
    """Read the saved password back, re-read the page, fill the password into it, and say what happened. The
    password is in 1Password already, so every outcome here names ``handle`` and a way to retry."""
    from tools import browser_vault_tool as vault

    done = {"saved": True, "handle": saved, "created": created}
    retry = ("Check the page: if it still shows the new-password form, call reset_password once more with this "
             "handle (it fills the same saved password). Use rotate=true only when the site refused that password, or "
             "symbols=false (also a new password) when it rejects symbols.")
    try:
        password = backend.resolve_password(saved)
    except Exception as exc:  # health: allow BLE001 -- typed or not, the password is saved and nothing is filled
        return _refuse(getattr(exc, "error_type", "vault_read_failed"), "The new password is saved in 1Password but "
                       "could not be read back, so nothing was filled. " + retry, **done)
    if (refusal := vault._register_redactions([password])) is not None:
        return _with(refusal, done, next=retry)
    try:
        now = vault._current_page_origin(tid)
    except vault._PageUnavailable as exc:
        return json.dumps({**exc.result(), **done, "next": retry})
    if now != origin:
        return _refuse("origin_changed", "The page left the reset page before the fill, so nothing was written "
                       "there. The new password is saved in 1Password under handle.", **done,
                       next="Open the reset link again, then call reset_password with this handle (it fills the "
                            "same saved password).")
    # The page may have changed while 1Password generated the password (an SPA turning the form into a sign-up): the
    # strict reset decision is made again on a fresh inspection, and that inspection is what is written.
    fresh, refusal = _reset_target(tid, " Nothing was filled into the page; the new password is saved in 1Password "
                                        "under handle.")
    if refusal:
        return _with(refusal, done, next="Do not submit. If this is the reset page again, call reset_password with "
                                         "this handle (it fills the same saved password).")
    nonce, bind, target, classified = fresh
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
        out = json.loads(signup.with_challenge(tid, signup.finish(
            answer.get("result"), backend.name, "login", origin, target, tokens, "reset")))
    out.update(done)
    if out.get("success"):
        out["next"] = ("Submit the form; the site then uses the new password, which is saved in 1Password under "
                       "handle. Sign in with fill and this handle.")
    else:
        out["error"] = out.get("error", "").replace(signup.NO_SUBMIT, "Nothing was submitted. Do not submit the form.")
        out["next"] = ("The new password is saved in 1Password under handle, but the page may not hold it. Do not "
                       "submit. " + retry)
    return json.dumps(out)


def _with(result: str, extra: dict[str, Any], **more: Any) -> str:
    return json.dumps({**json.loads(result), **extra, **more})


def _generate(backend: Any, handle: str, origin: str, identifier: str, label: str, length: int,
              symbols: bool) -> tuple[str, Optional[str]]:
    """``(handle, None)`` once 1Password holds a freshly generated password, else ``("", refusal)``: nothing is
    filled then."""
    from agent.vault_backends import BackendError, BackendTimeout, UnlockRequired

    not_filled = "Nothing was filled into the page, so the site's password is unchanged."
    try:
        return backend.generate_login(handle, origin=origin, username=identifier, title=label, length=length,
                                      symbols=symbols), None
    except BackendTimeout as exc:  # the request may have been applied: not "saved: false"
        return "", _refuse(exc.error_type, f"{exc.message()} The new password MAY have been saved. {not_filled}",
                           saved="unknown",
                           next="Call browser_vault action=list and look for a login for this site (a copy made "
                                "just now, or this handle with a changed password) before saving another one: if "
                                "there is one, call reset_password with its handle; only otherwise try again.")
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
# by the non-secret eval; Python keeps no value it does not store. The script checks the page origin itself, right
# before reading (a redirect after Python's own check must not supply another site's key).
_KEY_JS = r"""(() => {
  if (window.location.origin !== __ORIGIN__) return JSON.stringify({ moved: true });
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


def key_js(origin: str) -> str:
    return _KEY_JS.replace("__ORIGIN__", json.dumps(origin))


def _is_totp_uri(value: str) -> bool:
    try:
        return urlsplit(value).netloc.lower() == "totp"
    except ValueError:  # a malformed candidate (the parser's message may quote it) is no key: say nothing
        return False


def page_key(found: Any) -> tuple[str, list[str], str]:
    """``(value to store, every page form of it, "")`` or ``("", [], reason)``. A TOTP otpauth:// URI wins (it
    keeps the digits, period and algorithm); else the one base32 key the page shows as text, upper-cased and
    without spaces."""
    from agent.vault_backends.onepassword import totp_key

    found = found if isinstance(found, dict) else {}
    uris = [u for u in found.get("uris") or [] if isinstance(u, str) and _is_totp_uri(u) and _valid_key(totp_key(u))]
    texts = [k for k in found.get("keys") or [] if isinstance(k, str) and _valid_key(totp_key(k))]
    pool = uris or texts
    if not pool:
        return "", [], "no_key_on_page"
    if len({totp_key(v) for v in pool}) > 1:
        return "", [], "several_keys_on_page"
    key = totp_key(pool[0])
    return (pool[0] if uris else key), [*uris, *texts, key], ""


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
    from agent.vault_backends import BackendError, BackendTimeout, UnlockRequired
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
    read = vault._eval_js(tid, key_js(origin))
    found = vault._parse_json_result(read.get("result")) if read.get("success") else None
    if isinstance(found, str):
        found = vault._parse_json_result(found)
    if isinstance(found, dict) and found.get("moved"):
        return _refuse("origin_changed", "The page left the authenticator-setup page before the key was read. "
                       "Nothing was saved or entered.")
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
    except BackendTimeout as exc:  # the key may be stored: say so
        return _refuse(exc.error_type, f"{exc.message()} The key MAY have been saved. {not_entered}",
                       saved="unknown", next="Call enroll_totp once more: a key that was stored is not written twice.")
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
                       "the codes. If the page then shows backup or recovery codes, do not read the page or take a "
                       "screenshot: click acknowledge or continue, or navigate away." if entered.get("success") else
                       "The key is saved in 1Password. Call enter_code with this handle once the code field is ready, "
                       "and confirm the setup.")
    return json.dumps(entered)
