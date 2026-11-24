"""1Password logins and credit cards as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --categories Login`` and ``--categories "Credit Card"``.
Logins need a website. Cards are payment metadata with no merchant origin and
no card values. Resolve: ``op item get <id> --vault <vault>`` so service-account
authentication works as well as interactive sessions. Card fields come from the
item's own field ids and types, not from a login password.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_stdin_secret
from agent.vault_backends import unlock as _unlock
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        from agent.secret_scope import get_secret
        env_name = str(self.cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN")
        self._service_token = get_secret(env_name, "") or ""

    # ── auth ────────────────────────────────────────────────────────────────

    def _op(self) -> Path:
        op = find_op(str(self.cfg.get("binary_path") or ""))
        if op is None:
            raise RuntimeError("1Password CLI (op) not found — install it or set vault.onepassword.binary_path")
        return op

    def _env(self, session_token: Optional[str]) -> Dict[str, str]:
        from agent.secret_scope import get_secret
        env = {k: os.environ[k] for k in _OP_ENV_ALLOWLIST if k in os.environ and not k.startswith("OP_CONNECT_")}
        # Connect credentials outrank OP_SERVICE_ACCOUNT_TOKEN inside op, so they must come from the
        # profile's own secret scope like the service token does — never from the launch environment.
        for k in ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
            if v := get_secret(k, ""):
                env[k] = v
        env["NO_COLOR"] = "1"
        account = str(self.cfg.get("account") or "")
        if account:
            env["OP_ACCOUNT"] = account
        if self._service_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = self._service_token
        elif session_token:
            # op signin --raw prints the bare token; the env var name carries the account shorthand,
            # which op also accepts as plain OP_SESSION for the default account.
            env[f"OP_SESSION_{account}" if account else "OP_SESSION"] = session_token
        return env

    def is_unlocked(self) -> bool:
        return bool(self._service_token) or _unlock.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        """Mint a session token from the master password (consumed on stdin, never argv)."""
        generation = _unlock.begin_unlock(self.name)
        cmd = [str(self._op()), "signin", "--raw"]
        if account := str(self.cfg.get("account") or ""):
            cmd += ["--account", account]
        proc = run_with_stdin_secret(cmd, env=self._env(None), secret=master_password, timeout=_TIMEOUT, label="op")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"1Password unlock failed: {_scrub(proc.stderr or '')[:200] or 'no session token'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("1Password was locked while unlocking; try again")

    def _run(self, *args: str) -> str:
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                       timeout_message="op timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            if "session" in err.lower() or "sign in" in err.lower() or "not signed in" in err.lower():
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            raise RuntimeError(f"op failed: {err[:200]}")
        return proc.stdout or ""

    # ── backend contract ───────────────────────────────────────────────────
    def list_items(self) -> List[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        configured_vault = str(self.cfg.get("vault") or "").strip()
        seen: set[str] = set()
        out: List[VaultItemMeta] = []
        for category in ("Login", "Credit Card"):
            try:
                raw = self._list_category(category, configured_vault)
            except UnlockRequired:
                raise
            except Exception as exc:
                if category == "Login":
                    raise
                logger.warning("1Password %s list failed: %s", category, str(exc)[:200])
                continue
            for item in raw:
                if isinstance(item, dict):
                    self._append_listed_item(item, configured_vault, seen, out)
        return out

    def _list_category(self, category: str, configured_vault: str) -> List[dict]:
        args = ["item", "list", "--categories", category, "--format", "json"]
        if configured_vault:
            args += ["--vault", configured_vault]
        raw = json.loads(self._run(*args) or "[]")
        return raw if isinstance(raw, list) else []

    def _append_listed_item(self, item: dict, configured_vault: str, seen: set[str], out: List[VaultItemMeta]) -> None:
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            return
        vault = item.get("vault") if isinstance(item.get("vault"), dict) else {}
        vault_id = str(vault.get("id") or configured_vault).strip()
        handle = f"{self.prefix}{vault_id}:{item_id}" if vault_id else f"{self.prefix}{item_id}"
        if _is_credit_card(item):
            seen.add(item_id)
            # A saved website on a card is not a merchant binding. The checkout grant names the merchant.
            out.append(VaultItemMeta(
                id=handle, kind="payment", label=str(item.get("title") or "Card"),
                origin=None, created_at=str(item.get("created_at") or ""), allowed_origins=()))
            return
        urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
        origins = _all_origins(urls)
        if not origins:
            return
        seen.add(item_id)
        username = str(item.get("additional_information") or "").strip() or None
        out.append(VaultItemMeta(
            id=handle, kind="login", label=str(item.get("title") or origins[0]),
            origin=origins[0], created_at=str(item.get("created_at") or ""),
            identifier_type="username" if username else None, identifier=username,
            allowed_origins=_web_origins(origins)))

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        requested_vault, requested_item = _split_handle(handle, self.prefix)
        for meta in self.list_items():
            meta_vault, meta_item = _split_handle(meta.id, self.prefix)
            if meta_item == requested_item and (requested_vault is None or requested_vault == meta_vault):
                return meta
        return None

    def _resolve_handle(self, handle: str) -> Tuple[Optional[str], str]:
        vault_id, item_id = _split_handle(handle, self.prefix)
        if vault_id:
            return vault_id, item_id
        meta = self.get_meta(handle)
        if meta is not None:
            return _split_handle(meta.id, self.prefix)
        configured_vault = str(self.cfg.get("vault") or "").strip()
        return configured_vault or None, item_id

    def resolve_password(self, handle: str) -> str:
        vault_id, item_id = self._resolve_handle(handle)
        args = ["item", "get", item_id]
        if vault_id:
            args += ["--vault", vault_id]
        args += ["--fields", "label=password", "--reveal"]
        return self._run(*args).rstrip("\r\n")

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        try:
            vault_id, item_id = self._resolve_handle(handle)
            args = ["item", "get", item_id]
            if vault_id:
                args += ["--vault", vault_id]
            code = self._run(*args, "--otp").strip()
        except Exception:
            return None
        return code if code.isdigit() else None

    def resolve_secret(self, handle: str) -> Dict[str, str]:
        meta = self.get_meta(handle)
        if meta is None:
            raise RuntimeError("vault item disappeared before it could be read")
        if meta.kind != "payment":
            return {"password": self.resolve_password(handle)}
        vault_id, item_id = self._resolve_handle(handle)
        args = ["item", "get", item_id, "--format", "json", "--reveal"]
        if vault_id:
            args += ["--vault", vault_id]
        raw = self._run(*args)
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("1Password returned an unreadable card item") from None
        finally:
            raw = ""
        if not isinstance(item, dict):
            raise RuntimeError("1Password returned an unreadable card item")
        return payment_from_item(item)


def _is_credit_card(item: dict) -> bool:
    category = str(item.get("category") or "").upper().replace(" ", "_").replace("-", "_")
    return category in {"CREDIT_CARD", "CREDITCARD"}


def _web_origins(origins: List[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _all_origins(urls: List[str]) -> List[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: List[str] = []
    for u in urls:
        try:
            origin = normalize_origin(u)
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out


def _split_handle(handle: str, prefix: str) -> Tuple[Optional[str], str]:
    payload = handle[len(prefix):] if handle.startswith(prefix) else handle
    vault_id, separator, item_id = payload.partition(":")
    return (vault_id or None, item_id) if separator else (None, payload)


_CVC_IDS = {"cvv", "cvc", "verificationnumber", "securitycode"}
_CVC_LABEL = re.compile(r"verification|cvc|cvv|security code|card code", re.I)
_NUMBER_IDS = {"ccnum", "creditcardnumber", "cardnumber"}
_HOLDER_IDS = {"cardholder", "cardholdername", "ccname"}
_EXPIRY_IDS = {"expiry", "expirydate", "expiredate"}
_POSTAL_IDS = {"zip", "postal", "postalcode", "billingpostalcode"}


def _iter_fields(item: dict) -> Iterable[dict]:
    for field in item.get("fields") or []:
        if isinstance(field, dict):
            yield field
    for section in item.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for field in section.get("fields") or []:
            if isinstance(field, dict):
                yield field


def _field_text(field: dict) -> str:
    value = field.get("value")
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def split_expiry(value: str) -> Optional[Tuple[str, str]]:
    """Return ``(month, year)`` from a 1Password month/year value. ``YYYYMM`` is the CLI's form."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 6 and int(digits[:4]) >= 2000:
        month, year = digits[4:6], digits[:4]
    elif len(digits) == 4:
        month, year = digits[:2], "20" + digits[2:]
    else:
        return None
    if not 1 <= int(month) <= 12:
        return None
    return month, year


def payment_from_item(item: dict) -> Dict[str, str]:
    """Map a revealed Credit Card item onto ``PAYMENT_FIELDS`` keys. Never reads a login password or PIN."""
    number = holder = cvc = expiry = postal = ""
    for field in _iter_fields(item):
        fid = re.sub(r"[^a-z0-9]", "", str(field.get("id") or "").lower())
        label = str(field.get("label") or "")
        ftype = str(field.get("type") or "").upper()
        text = _field_text(field)
        if not text or fid == "pin":
            continue
        if ftype == "CREDIT_CARD_NUMBER" or fid in _NUMBER_IDS:
            number = re.sub(r"[\s-]", "", text)
        elif fid in _CVC_IDS or (ftype == "CONCEALED" and _CVC_LABEL.search(label)):
            cvc = re.sub(r"\s+", "", text)
        elif fid in _EXPIRY_IDS or (
            ftype == "MONTH_YEAR" and "valid" not in fid and "valid" not in label.lower()
            and "expir" in f"{fid} {label.lower()}"
        ):
            expiry = text
        elif fid in _HOLDER_IDS or "cardholder" in label.lower():
            holder = text
        elif fid in _POSTAL_IDS or "postal" in label.lower() or label.lower() == "zip":
            postal = text
    parsed = split_expiry(expiry)
    out: Dict[str, str] = {}
    if number:
        out["card_number"] = number
    if holder:
        out["cardholder_name"] = holder
    if parsed:
        out["exp_month"], out["exp_year"] = parsed
    if cvc:
        out["cvc"] = cvc
    if postal:
        out["billing_postal_code"] = postal
    missing = [name for name in ("card_number", "exp_month", "exp_year", "cvc") if name not in out]
    if missing:
        raise RuntimeError("card item is missing required fields: " + ", ".join(missing))
    return out
