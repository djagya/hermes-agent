"""1Password Login items as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --categories Login,Credit Card --format json`` → title, urls,
username, vault. Resolve: ``op item get <id> --vault <vault> ...`` so
service-account authentication works as well as interactive sessions.

Credit Card items are ``kind=payment`` and are fillable only where the user bound them IN 1Password:
the item's primary https website is the merchant page origin; every other https website is a payment
frame origin (a PSP's hosted card fields, e.g. Datatrans). A card with no https website is listed but
never fillable. Card handles are vault-qualified like Login handles. Card values are read with
``op item get <id> --vault <vault> --format json --reveal`` (stdout pipe only) and mapped to the
vault's canonical ``PAYMENT_FIELDS``; nothing but field names ever reaches an error.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_stdin_secret
from agent.vault_backends import unlock as _unlock
from agent.vault_store import REQUIRED_FIELDS, VaultItemMeta, normalize_origin

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
        args = ["item", "list", "--categories", "Login,Credit Card", "--format", "json"]
        configured_vault = str(self.cfg.get("vault") or "").strip()
        if configured_vault:
            args += ["--vault", configured_vault]
        raw = json.loads(self._run(*args) or "[]")
        out: List[VaultItemMeta] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "LOGIN").upper()
            if category == _CARD_CATEGORY:
                if handle := self._handle(item, configured_vault):
                    out.append(self._card_meta(item, handle))
                continue
            if category != "LOGIN":
                continue
            urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
            origin = _first_origin(urls)
            if not origin:
                continue
            handle = self._handle(item, configured_vault)
            if not handle:
                continue
            username = str(item.get("additional_information") or "").strip() or None
            out.append(VaultItemMeta(
                id=handle, kind="login", label=str(item.get("title") or origin),
                origin=origin, created_at=str(item.get("created_at") or ""),
                identifier_type="username" if username else None, identifier=username))
        return out

    def _handle(self, item: Dict, configured_vault: str) -> Optional[str]:
        """Vault-qualified ``op:<vault>:<item>`` handle (bare ``op:<item>`` only when op reports no vault)."""
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            return None
        vault = item.get("vault") if isinstance(item.get("vault"), dict) else {}
        vault_id = str(vault.get("id") or configured_vault).strip()
        return f"{self.prefix}{vault_id}:{item_id}" if vault_id else f"{self.prefix}{item_id}"

    def _card_meta(self, item: Dict, handle: str) -> VaultItemMeta:
        """Metadata for a Credit Card item. ``additional_information`` (op's masked number) is never
        surfaced; the binding comes only from the item's own https websites."""
        merchant, frames = _card_binding(item.get("urls") or [])
        return VaultItemMeta(
            id=handle, kind="payment", label=str(item.get("title") or "Credit card"),
            origin=merchant, created_at=str(item.get("created_at") or ""), frame_origins=frames)

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

    def resolve_secret(self, handle: str) -> Dict[str, str]:
        """Card payload for a Credit Card handle, password shape otherwise. Raises RuntimeError naming only
        missing FIELD NAMES when the item lacks a required card value."""
        vault_id, item_id = self._resolve_handle(handle)
        args = ["item", "get", item_id]
        if vault_id:
            args += ["--vault", vault_id]
        raw = self._run(*args, "--format", "json", "--reveal")
        try:
            item = json.loads(raw or "{}")
        except ValueError:
            raise RuntimeError("op returned an unreadable item") from None
        finally:
            del raw
        if str(item.get("category") or "").upper() != _CARD_CATEGORY:
            return {"password": self.resolve_password(handle)}
        card = card_payload(item.get("fields") or [])
        item.clear()
        missing = [f for f in REQUIRED_FIELDS["payment"] if not card.get(f)]
        if missing:
            card.clear()
            raise RuntimeError(f"1Password card item is missing: {', '.join(missing)}")
        return card

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


_CARD_CATEGORY = "CREDIT_CARD"

# 1Password Credit Card template: field id first (stable), then its default label.
_CARD_FIELD_KEYS = {
    "card_number": ("ccnum", "number"),
    "cvc": ("cvv", "verification number"),
    "cardholder_name": ("cardholder", "cardholder name"),
    "expiry": ("expiry", "expiry date"),
}


def _card_binding(urls: List) -> Tuple[Optional[str], Tuple[str, ...]]:
    """(merchant origin, payment frame origins) from a card item's websites. https only: a card is never
    bound to a plaintext origin. The primary website (else the first https one) is the merchant."""
    entries = []
    for u in urls:
        if not isinstance(u, dict) or not u.get("href"):
            continue
        try:
            origin = normalize_origin(str(u["href"]))
        except Exception:
            continue
        if origin.startswith("https://"):
            entries.append((bool(u.get("primary")), origin))
    if not entries:
        return None, ()
    merchant = next((o for primary, o in entries if primary), entries[0][1])
    frames = tuple(dict.fromkeys(o for _, o in entries if o != merchant))
    return merchant, frames


def _parse_expiry(value: str) -> Optional[Tuple[str, str]]:
    """(month, 4-digit year) from op's MONTH_YEAR value (``YYYYMM``) or a typed ``MM/YYYY`` / ``MM/YY``."""
    v = value.strip()
    m = re.fullmatch(r"(\d{4})(\d{2})", v) or re.fullmatch(r"(\d{4})[/-](\d{1,2})", v)
    if m:
        year, month = m.group(1), m.group(2)
    else:
        m = re.fullmatch(r"(\d{1,2})\s*[/-]\s*(\d{2}|\d{4})", v)
        if not m:
            return None
        month, year = m.group(1), m.group(2)
        year = year if len(year) == 4 else f"20{year}"
    return (str(int(month)), year) if 1 <= int(month) <= 12 else None


def card_payload(fields: List) -> Dict[str, str]:
    """Map a 1Password Credit Card item's ``fields`` onto the vault's canonical PAYMENT_FIELDS names."""
    by_id: Dict[str, str] = {}
    by_label: Dict[str, str] = {}
    for f in fields:
        if not isinstance(f, dict) or f.get("value") in (None, ""):
            continue
        by_id.setdefault(str(f.get("id") or "").lower(), str(f["value"]))
        by_label.setdefault(str(f.get("label") or "").strip().lower(), str(f["value"]))
    out: Dict[str, str] = {}
    for name, (fid, label) in _CARD_FIELD_KEYS.items():
        value = by_id.get(fid) or by_label.get(label)
        if not value:
            continue
        if name == "expiry":
            parsed = _parse_expiry(value)
            if parsed:
                out["exp_month"], out["exp_year"] = parsed
        elif name == "card_number":
            out[name] = re.sub(r"[\s-]", "", value)
        else:
            out[name] = value.strip()
    by_id.clear()
    by_label.clear()
    return out


def _first_origin(urls: List[str]) -> Optional[str]:
    for u in urls:
        try:
            return normalize_origin(u)
        except Exception:
            continue
    return None


def _split_handle(handle: str, prefix: str) -> Tuple[Optional[str], str]:
    payload = handle[len(prefix):] if handle.startswith(prefix) else handle
    vault_id, separator, item_id = payload.partition(":")
    return (vault_id or None, item_id) if separator else (None, payload)
