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
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import (
    BackendAuthExpired,
    BackendError,
    BackendNetwork,
    BackendRateLimited,
    BackendTimeout,
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
_skipped_cache: dict[tuple, tuple] = {}  # same keys: items the listing skipped (title + reason)

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
        _skipped_cache.clear()


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True
    payment_revisions = True  # every card fill binds and re-checks the item revision (version / updated_at)

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        from agent.secret_scope import get_secret
        env_name = str(self.cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN")
        self._service_token = get_secret(env_name, "") or ""
        self._item_vaults: dict[str, str] = {}  # item id -> vault id from the latest ``item list``

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

    def _run(self, *args: str, secret: tuple[str, ...] = ()) -> str:
        """op's stdout; a failure raises its typed cause with op's stderr (each ``secret``, a value a write sent in
        argv, cut out of it in any case before it is kept). The raised error is made after the failed call's own
        exception is gone, so nothing reachable from it (``__context__``, ``__cause__``) holds the argv."""
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc, failure = None, ""
        try:
            proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                           timeout_message="op timed out", stdin=subprocess.DEVNULL)
        except RuntimeError as exc:  # its cause (TimeoutExpired) carries the argv: keep only the message
            failure = str(exc)
        if proc is None:
            if "timed out" in failure:
                raise BackendTimeout(self.display_name, f"op timed out after {_TIMEOUT:.0f}s")
            raise RuntimeError(failure)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            for value in filter(None, secret):
                err = re.sub(re.escape(value), "[REDACTED]", err, flags=re.IGNORECASE)
            raise self._classify(err)
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
            # A fresh backend (one per tool call) must still prove handles from this listing.
            self._item_vaults = {}
            for meta in hit[1]:
                if meta.kind == "login":
                    vault_id, item_id = _split_handle(meta.id, self.prefix)
                    self._item_vaults[item_id] = vault_id or ""
            with _list_cache_lock:
                self.skipped = _skipped_cache.get(key, ())
            return list(hit[1])
        items = self._list_items_uncached()
        with _list_cache_lock:
            if len(_list_cache) > 32:
                _list_cache.clear()
                _skipped_cache.clear()
            _list_cache[key] = (now, list(items))
            _skipped_cache[key] = tuple(self.skipped)
        return items

    def invalidate_listing(self) -> None:
        clear_list_cache()

    def _list_items_uncached(self) -> list[VaultItemMeta]:
        args = ["item", "list", "--categories", "Login,Credit Card", "--format", "json"]
        configured_vault = str(self.cfg.get("vault") or "").strip()
        if configured_vault:
            args += ["--vault", configured_vault]
        raw = json.loads(self._run(*args) or "[]")
        out: list[VaultItemMeta] = []
        skipped: list[dict] = []
        self._item_vaults = {}
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
                        payment_scope="confirmed_page",  # hosted origins: the reviewed set at fill time
                        revision=_item_revision(item)))
                continue
            urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
            origins = _all_origins(urls)
            if not origins:
                if urls:  # a website the fill cannot bind (unparseable): say so instead of hiding it
                    skipped.append({"title": str(item.get("title") or "")[:120], "reason": "website_unreadable"})
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            vault = item.get("vault") if isinstance(item.get("vault"), dict) else {}
            vault_id = str(vault.get("id") or configured_vault).strip()
            handle = f"{self.prefix}{vault_id}:{item_id}" if vault_id else f"{self.prefix}{item_id}"
            self._item_vaults[item_id] = vault_id
            username = str(item.get("additional_information") or "").strip() or None
            out.append(VaultItemMeta(
                id=handle, kind="login", label=str(item.get("title") or origins[0]),
                origin=origins[0], created_at=str(item.get("created_at") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=_web_origins(origins)))
        self.skipped = tuple(skipped[:50])
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        requested_vault, requested_item = _split_handle(handle, self.prefix)
        for meta in self.list_items():
            meta_vault, meta_item = _split_handle(meta.id, self.prefix)
            if meta_item == requested_item and (requested_vault is None or requested_vault == meta_vault):
                return meta
        return None

    def _resolve_handle(self, handle: str) -> tuple[Optional[str], str]:
        """``(vault, item)`` for an ``op item get``. A legacy (unqualified) handle takes the vault its
        listing reported, listing at most once per backend; a handle no listing returned is refused
        before any secret read, so a model-supplied id never reaches op's argv unproven."""
        vault_id, item_id = _split_handle(handle, self.prefix)
        if vault_id:
            if vault_id.startswith("-") or not item_id or item_id.startswith("-"):
                raise RuntimeError("1Password login not found; list logins again")
            return vault_id, item_id
        if item_id not in self._item_vaults:
            self.list_items()
        if item_id not in self._item_vaults:
            raise RuntimeError("1Password login not found; list logins again")
        return self._item_vaults[item_id] or None, item_id

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

    def recovery_fields(self, handle: str, reveal: bool = False) -> list:
        """D-0365: the Login item's recovery-code fields, each with the item's revision at this read. The read
        before the approval runs ``op item get`` without ``--reveal`` and keeps no value, whatever op printed
        (the ``payment_revision`` precedent); the read after it reveals, and the caller takes only the field the
        approval named, at the same revision (``bound_codes``)."""
        from agent.vault_recovery_codes import RecoveryField, is_recovery_code_label

        vault_id, item_id = self._resolve_handle(handle)
        args = ["item", "get", item_id]
        if vault_id:
            args += ["--vault", vault_id]
        try:
            item = json.loads(self._run(*args, "--format", "json", *(["--reveal"] if reveal else [])))
        except ValueError:
            raise BackendError(self.display_name, "op item get returned no JSON item") from None
        fields = item.get("fields") if isinstance(item, dict) else None
        revision = _item_revision(item)
        out = []
        for f in fields if isinstance(fields, list) else []:
            if not isinstance(f, dict):
                continue
            # The login's own password, username and TOTP field are never codes, whatever their label says.
            protected = (str(f.get("purpose") or "").upper() in ("PASSWORD", "USERNAME")
                         or str(f.get("type") or "").upper() == "OTP" or str(f.get("id") or "") == "password")
            field_id, label, value = f.get("id"), f.get("label"), f.get("value") if reveal else ""
            if protected or not (isinstance(field_id, str) and field_id) or not isinstance(label, str):
                continue
            if is_recovery_code_label(label):
                out.append((RecoveryField(field_id, label, revision), value if isinstance(value, str) else ""))
        return out

    def resolve_secret(self, handle: str) -> dict[str, str]:
        """Reveal a single card only inside the trusted native filler, never in tool output."""
        return self.resolve_payment_secret(handle)[0]

    def resolve_payment_secret(self, handle: str) -> tuple[dict[str, str], Optional[str]]:
        """The card and the ``version`` of the item op revealed (same JSON). The fill compares it with the
        version the prompt was shown for, so an item edited meanwhile is never written."""
        from agent.vault_payment_card import CardInvalid, parse_onepassword_card

        vault_id, item_id = _split_handle(handle, self.prefix)
        if not vault_id:
            raise ValueError("A vault-qualified card handle is required")
        try:
            raw = self._run("item", "get", item_id, "--vault", vault_id, "--format", "json", "--reveal")
            item = json.loads(raw)
            return parse_onepassword_card(item, item_id=item_id, vault_id=vault_id), _item_revision(item)
        except (UnlockRequired, ItemNotFound, BackendRateLimited, BackendAuthExpired, BackendNetwork):
            raise  # typed causes carry no card data, only the scrubbed CLI error
        except CardInvalid as exc:  # dlz (11-18): the same fixed text, with the check's fixed code
            raise CardInvalid("Payment card resolution failed", exc.code) from None
        except Exception:
            raise ValueError("Payment card resolution failed") from None

    def payment_revision(self, meta: VaultItemMeta) -> str:
        """dlz (design v2 A8): version-based binding when op can give one. The listing's revision when it
        carries ``op.version``; else one metadata ``op item get`` (no ``--reveal``; only ``version`` and
        ``updated_at`` are kept, the rest is dropped, never logged or returned) for the item's version, the same JSON shape
        the reveal returns. Neither has a version: the listing's ``updated_at``-only revision, which the fill
        result reports as ``updated_at_only``. A lookup that fails refuses (typed or fixed error, no output)."""
        from agent.vault_store import revision_binding

        listed = str(getattr(meta, "revision", "") or "")
        if revision_binding(listed) == "version":
            return listed
        vault_id, item_id = _split_handle(str(getattr(meta, "id", "") or ""), self.prefix)
        if not vault_id or not item_id or vault_id.startswith("-") or item_id.startswith("-"):
            raise ValueError("A vault-qualified card handle is required")
        try:
            looked = _item_revision(json.loads(self._run("item", "get", item_id, "--vault", vault_id,
                                                         "--format", "json")))
        except (UnlockRequired, ItemNotFound, BackendRateLimited, BackendAuthExpired, BackendNetwork):
            raise  # typed causes carry no card data, only the scrubbed CLI error
        except Exception:
            raise BackendError(self.display_name, "card revision lookup failed") from None
        return looked if revision_binding(looked) == "version" else listed

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

    # ── writes (D-1085 authenticator enrolment, D-1086 password reset) ─────
    def generate_login(self, handle: str, *, origin: str, username: str = "", title: str = "", length: int = 20,
                       symbols: bool = True) -> str:
        """Give the Login item ``handle`` a password op generates (the item's other fields, passkey and files are
        untouched: ``op item edit --generate-password`` sets only the password field), or, without a handle, create
        a Login item for ``origin`` in the configured vault (default ``Hermes``) with one. Returns the item's
        handle. No password crosses this process: op generates it and :meth:`resolve_password` reads it back."""
        recipe = "--generate-password=letters,digits" + (",symbols" if symbols else "") + f",{length}"  # op help order
        if handle:
            vault_id, item_id = self._resolve_handle(handle)
            self._run("item", "edit", item_id, *(["--vault", vault_id] if vault_id else []), recipe)
            self.invalidate_listing()
            return f"{self.prefix}{vault_id}:{item_id}" if vault_id else handle
        vault = str(self.cfg.get("vault") or "").strip() or "Hermes"
        out = self._run("item", "create", "--category", "login", "--vault", vault, "--title",
                        title or origin.split("://")[-1], "--url", origin, recipe, "--format", "json",
                        f"username={username}")
        self.invalidate_listing()
        try:
            created = json.loads(out)
        except ValueError:
            created = None
        created = created if isinstance(created, dict) else {}
        vault_ref = created.get("vault") if isinstance(created.get("vault"), dict) else {}
        item_id, vault_id = str(created.get("id") or ""), str(vault_ref.get("id") or "")
        if not item_id or not vault_id:
            raise BackendError(self.display_name, "op item create returned no item id")
        return f"{self.prefix}{vault_id}:{item_id}"

    def add_totp(self, handle: str, seed: str) -> bool:
        """Store ``seed`` (an otpauth:// URI or a base32 key) in the item's "Hermes authenticator" one-time-password
        field with one ``op item edit`` field assignment: op changes that field only, so a passkey, files and every
        other field stay as they are. False when the item already holds this key (nothing written). A key in any
        other one-time-password field is never touched (:class:`TotpAlreadySet`); a different key in Hermes's own
        field (an enrolment the site may never have confirmed) is replaced, and in the same call kept in a concealed
        "Hermes previous authenticator key <hash>" field (its own label per old key, so a later replace keeps it
        too). The key is in op's argv for that one call only."""
        vault_id, item_id = self._resolve_handle(handle)
        where = ["--vault", vault_id] if vault_id else []
        fields = [f for f in self._revealed(item_id, vault_id).get("fields") or [] if isinstance(f, dict)]
        held = [f for f in fields if str(f.get("type") or "").upper() == "OTP" and str(f.get("value") or "").strip()]
        if any(totp_key(str(f["value"])) == totp_key(seed) for f in held):
            return False
        mine = [f for f in held if _is_hermes_field(f)]
        if len(mine) != len(held):
            raise TotpAlreadySet(self.display_name)
        if any(not (_is_hermes_field(f) and str(f.get("type") or "").upper() == "OTP")
               for f in fields if _op_would_match(f)):
            raise TotpFieldConflict(self.display_name)  # the assignment would overwrite and retype that field
        old = str(mine[0]["value"]) if mine else ""
        assign = [f"{_TOTP_LABEL}[otp]={seed}"] + ([f"{_previous_label(old)}[password]={old}"] if old else [])
        self._run("item", "edit", item_id, *where, *assign, secret=(seed, totp_key(seed), old, totp_key(old)))
        self.invalidate_listing()
        return True

    def _revealed(self, item_id: str, vault_id: Optional[str]) -> dict:
        """The item with its field values (read only: no write sends it back)."""
        raw = self._run("item", "get", item_id, *(["--vault", vault_id] if vault_id else []), "--format", "json",
                        "--reveal")
        try:
            item = json.loads(raw)
        except ValueError:
            item = None
        if not isinstance(item, dict):
            raise BackendError(self.display_name, "op item get returned no JSON item")
        return item


# Labels of the fields add_totp writes (no ".", "=" or backslash, which op's assignment syntax would need
# escaped). The field is Hermes's by its label at item level: op assigns the ids.
_TOTP_LABEL = "Hermes authenticator"
_PREVIOUS_LABEL = "Hermes previous authenticator key"


def _previous_label(old: str) -> str:
    """The field that keeps a replaced key: labelled with a short hash of that key (hex: nothing to escape)."""
    return f"{_PREVIOUS_LABEL} {hashlib.sha256(totp_key(old).encode()).hexdigest()[:8]}"


def _is_hermes_field(field: dict) -> bool:
    section = field.get("section") if isinstance(field.get("section"), dict) else {}
    return field.get("label") == _TOTP_LABEL and not section.get("label")


def _op_would_match(field: Any) -> bool:
    """``op item edit 'Hermes authenticator[otp]=...'`` (no section) takes any field in any section whose label
    equals the name ignoring spaces, or whose id is that name. Case: op's rule is not checked here, so any case
    counts (a field too many is refused, never overwritten)."""
    squash = lambda text: str(text or "").replace(" ", "").casefold()  # noqa: E731
    return isinstance(field, dict) and (squash(field.get("label")) == squash(_TOTP_LABEL)
                                        or field.get("id") == _TOTP_LABEL)


class TotpFieldConflict(BackendError):
    error_type = "totp_field_conflict"
    advice = ("this login already has a field named \"Hermes authenticator\" that is not a one-time-password field "
              "Hermes wrote, and saving the key would overwrite it, so nothing was saved or entered. Tell the user.")


class TotpAlreadySet(BackendError):
    error_type = "totp_already_set"
    advice = ("this login already has a different authenticator key, and the site may still use it, so nothing was "
              "saved or entered. Use enter_code with this handle for its codes; replacing an authenticator is the "
              "user's call.")


def totp_key(value: str) -> str:
    """The base32 key of an otpauth:// URI or a bare key, upper-cased, without spaces, dashes or padding."""
    from urllib.parse import parse_qs, urlsplit

    if value.strip().lower().startswith("otpauth://"):
        try:
            value = (parse_qs(urlsplit(value.strip()).query).get("secret") or [""])[0]
        except ValueError:  # a malformed candidate is no key; the parser's message may quote it, so say nothing
            return ""
    return "".join(value.split()).replace("-", "").rstrip("=").upper()


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


def _item_revision(item: object) -> str:
    """The item's revision from metadata only (never a field value): ``op.version`` (1Password bumps it on
    every edit) and ``op.updated_at``, each when op reported it. The same function reads the listing, the
    metadata lookup (``payment_revision``) and the revealed item; ``revision_verdict`` needs the same parts on
    both sides, so a part missing from one response is never compared away. "" = neither."""
    if not isinstance(item, dict):
        return ""
    parts = []
    version = item.get("version")
    if type(version) is int and version >= 0:
        parts.append(f"op.version={version}")
    elif isinstance(version, str) and version.isascii() and version.isdecimal():
        parts.append(f"op.version={int(version)}")
    updated = item.get("updated_at")
    if isinstance(updated, str) and 0 < len(updated) <= 64 and not set(updated) & set(";= \t\r\n"):
        parts.append(f"op.updated_at={updated}")
    return ";".join(parts)


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
        u = u.strip()
        if u and "://" not in u:
            u = "https://" + u  # dlz (11-18): "pypi.org" is read as https://pypi.org, as 1Password itself does
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
