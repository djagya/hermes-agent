"""1Password Login items as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --categories Login --format json`` → title, urls,
username, vault. Resolve: ``op item get <id> --vault <vault> ...`` so
service-account authentication works as well as interactive sessions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import (
    BackendAuthExpired,
    BackendError,
    BackendNetwork,
    BackendRateLimited,
    ItemNotFound,
    LoginBackend,
    UnlockRequired,
    run_with_stdin_secret,
)
from agent.vault_backends import unlock as _unlock
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
# ``op item list`` ran on every fill and every get_meta. 1Password rate-limits
# and budgets service-account requests, so the item METADATA list (titles,
# URLs, usernames, ids; never a password, card or TOTP value) is reused for
# this long. A lock, a profile switch or a different account/vault misses.
LIST_CACHE_TTL_S = 60.0
_list_cache_lock = threading.Lock()
_list_cache: dict[tuple, tuple[float, list]] = {}

# Lower-cased ``op`` stderr fragments -> typed failure. Checked in this order.
_RATE_MARKERS = ("rate limit", "too many requests", "daily limit", "request limit", "quota")
_NOT_FOUND_MARKERS = ("isn't an item", "is not an item", "no item found", "item not found", "could not find item",
                      "isn't a vault", "vault not found", "doesn't exist", "does not exist")
_AUTH_MARKERS = ("unauthorized", "invalid token", "token expired", "expired token", "token has expired",
                 "revoked", "invalid bearer", "authentication failed", "service account is not active")
_SESSION_MARKERS = ("session", "sign in", "not signed in")
_NETWORK_MARKERS = ("dial tcp", "no such host", "connection refused", "connection reset", "network is unreachable",
                    "i/o timeout", "timed out", "timeout", "tls handshake", "could not connect", "eof",
                    "temporary failure in name resolution")


def clear_list_cache() -> None:
    with _list_cache_lock:
        _list_cache.clear()


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True

    def __init__(self, cfg: Optional[dict] = None):
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

    def _env(self, session_token: Optional[str]) -> dict[str, str]:
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
        try:
            proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                           timeout_message="op timed out", stdin=subprocess.DEVNULL)
        except RuntimeError as exc:
            if "timed out" in str(exc):
                raise BackendNetwork(self.display_name, f"op timed out after {_TIMEOUT:.0f}s") from None
            raise
        if proc.returncode != 0:
            raise self._classify(_scrub(proc.stderr or ""))
        return proc.stdout or ""

    def _classify(self, err: str) -> Exception:
        """The typed failure for an ``op`` error (``UnlockRequired`` locks the interactive session)."""
        low = err.lower()
        if any(m in low for m in _RATE_MARKERS):
            return BackendRateLimited(self.display_name, err)
        auth = any(m in low for m in _AUTH_MARKERS) or any(m in low for m in _SESSION_MARKERS)
        if auth and self._service_token:
            # A service-account / Connect token cannot be fixed by an interactive unlock.
            return BackendAuthExpired(self.display_name, err)
        if auth:
            _unlock.lock(self.name)
            return UnlockRequired(self)
        if any(m in low for m in _NOT_FOUND_MARKERS):
            return ItemNotFound(self.display_name, err)
        if any(m in low for m in _NETWORK_MARKERS):
            return BackendNetwork(self.display_name, err)
        return BackendError(self.display_name, f"op failed: {err}")

    def _list_cache_key(self) -> tuple:
        home, generation = _unlock.generation(self.name)
        token = hashlib.sha256(self._service_token.encode()).hexdigest()[:16] if self._service_token else ""
        return (home, generation, str(self.cfg.get("account") or ""), str(self.cfg.get("vault") or "").strip(), token)

    # ── backend contract ───────────────────────────────────────────────────
    def list_items(self) -> list[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        key = self._list_cache_key()
        now = time.monotonic()
        with _list_cache_lock:
            hit = _list_cache.get(key)
        if hit is not None and now - hit[0] < LIST_CACHE_TTL_S:
            return list(hit[1])
        items = self._list_items_uncached()
        with _list_cache_lock:
            if len(_list_cache) > 32:
                _list_cache.clear()
            _list_cache[key] = (now, list(items))
        return items

    def _list_items_uncached(self) -> list[VaultItemMeta]:
        args = ["item", "list", "--categories", "Login,Credit Card", "--format", "json"]
        configured_vault = str(self.cfg.get("vault") or "").strip()
        if configured_vault:
            args += ["--vault", configured_vault]
        raw = json.loads(self._run(*args) or "[]")
        out: list[VaultItemMeta] = []
        for item in raw if isinstance(raw, list) else []:
            if item.get("category") == "CREDIT_CARD":
                from agent.vault_payment_policy import onepassword_payment_metadata

                payment = onepassword_payment_metadata(item, configured_vault)
                if payment is not None:
                    # Cards are selected by handle, then authorized for the inspected
                    # page in the human prompt. A bank website is not a merchant binding.
                    out.append(VaultItemMeta(
                        id=payment.handle, kind="payment", label=str(item.get("title") or "Payment card"),
                        origin=None, created_at=str(item.get("created_at") or ""),
                        payment_scope="confirmed_page"))  # hosted origins: the reviewed set at fill time
                continue
            urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
            origins = _all_origins(urls)
            if not origins:
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            vault = item.get("vault") if isinstance(item.get("vault"), dict) else {}
            vault_id = str(vault.get("id") or configured_vault).strip()
            handle = f"{self.prefix}{vault_id}:{item_id}" if vault_id else f"{self.prefix}{item_id}"
            username = str(item.get("additional_information") or "").strip() or None
            out.append(VaultItemMeta(
                id=handle, kind="login", label=str(item.get("title") or origins[0]),
                origin=origins[0], created_at=str(item.get("created_at") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=_web_origins(origins)))
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        requested_vault, requested_item = _split_handle(handle, self.prefix)
        for meta in self.list_items():
            meta_vault, meta_item = _split_handle(meta.id, self.prefix)
            if meta_item == requested_item and (requested_vault is None or requested_vault == meta_vault):
                return meta
        return None

    def _resolve_handle(self, handle: str) -> tuple[Optional[str], str]:
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
        # The whole item as JSON, not ``--fields label=password``: that plain output is CSV-quoted, so a
        # password holding a quote or comma came back wrapped in literal quotes and the fill was refused.
        raw = self._run(*args, "--format", "json", "--reveal")
        try:
            item = json.loads(raw)
        except ValueError:
            raise BackendError(self.display_name, "op item get returned no JSON item") from None
        return _login_password(item, self.display_name)

    def resolve_secret(self, handle: str) -> dict[str, str]:
        """Reveal a single card only inside the trusted native filler, never in tool output."""
        from agent.vault_payment_card import parse_onepassword_card

        vault_id, item_id = _split_handle(handle, self.prefix)
        if not vault_id:
            raise ValueError("A vault-qualified card handle is required")
        try:
            raw = self._run("item", "get", item_id, "--vault", vault_id, "--format", "json", "--reveal")
            return parse_onepassword_card(json.loads(raw), item_id=item_id, vault_id=vault_id)
        except (UnlockRequired, ItemNotFound, BackendRateLimited, BackendAuthExpired, BackendNetwork):
            raise  # typed causes carry no card data, only the scrubbed CLI error
        except Exception:
            raise ValueError("Payment card resolution failed") from None

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        # A typed outage (rate budget, expired token, network, missing item) is raised so the caller does not
        # tell the model to "save an authenticator key" for a login that already has one.
        try:
            vault_id, item_id = self._resolve_handle(handle)
            args = ["item", "get", item_id]
            if vault_id:
                args += ["--vault", vault_id]
            code = self._run(*args, "--otp").strip()
        except BackendError as exc:
            if _no_otp_field(exc.detail) or type(exc) is BackendError:
                return None  # the item has no TOTP seed: the user is asked instead
            raise
        except Exception:
            return None
        return code if code.isdigit() else None


def _login_password(item: object, backend: str) -> str:
    """The Login item's password value, byte-exact (no trim, no CSV decoding).

    Picks the field 1Password marks ``purpose: PASSWORD``, then the built-in ``id: password``,
    then a field labelled ``password``. Several matching fields in the first tier that has any
    are fine when they agree; different values are refused rather than guessed.
    """
    fields = item.get("fields") if isinstance(item, dict) else None
    fields = [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []
    tiers = (
        [f for f in fields if f.get("purpose") == "PASSWORD"],
        [f for f in fields if f.get("id") == "password"],
        [f for f in fields if str(f.get("label") or "").strip().lower() == "password"],
    )
    for tier in tiers:
        values = {f.get("value") for f in tier if isinstance(f.get("value"), str) and f.get("value") != ""}
        if len(values) == 1:
            return values.pop()
        if len(values) > 1:
            raise BackendError(backend, "the item has several different password fields; keep one")
    raise BackendError(backend, "the item has no password field")


def _no_otp_field(detail: str) -> bool:
    """op's "this item has no one-time password" refusal (not an outage)."""
    low = (detail or "").lower()
    return "one-time password" in low and any(m in low for m in ("no ", "doesn't have", "does not have", "not have"))


def _web_origins(origins: list[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _all_origins(urls: list[str]) -> list[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: list[str] = []
    for u in urls:
        try:
            origin = normalize_origin(u)
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out


def _split_handle(handle: str, prefix: str) -> tuple[Optional[str], str]:
    payload = handle.removeprefix(prefix)
    vault_id, separator, item_id = payload.partition(":")
    return (vault_id or None, item_id) if separator else (None, payload)
