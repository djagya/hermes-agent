"""1Password (`op` CLI) secret source.

Users map env-var names to ``op://vault/item/field`` references in
``secrets.onepassword.env``. Two or more references into the same item are
resolved with ONE ``op item get`` (1Password rate-limits service accounts per
request, and a 20-field item would otherwise cost 20 ``op read`` calls); a lone
reference, or one the item JSON can't answer unambiguously, uses
``op read -- <ref>``. Auth is whatever the user's ``op`` already has
(``OP_SERVICE_ACCOUNT_TOKEN`` headless, ``OP_SESSION_*`` interactive) — Hermes
never authenticates on the user's behalf, and failures never block startup.
Complete pulls are cached in-process and under
``<hermes_home>/cache/op_cache.json`` (values only; auth material is
fingerprinted, never stored).

On a rate-limit response the pull stops at once, a backoff marker
(``op_rate_limit.json``, no secret values) makes every later process skip
``op`` until it expires, and the last complete pull is served for the missing
names — so a throttled account is not kept throttled by restarts and CLI runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess  # noqa: F401 — tests monkeypatch ``op.subprocess.run``
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.secret_sources._cache import (
    CachedFetch, SecretCache, atomic_write_json, fingerprint as _fingerprint, resolve_cache_home,
)
from agent.secret_sources.base import (
    ErrorKind, FetchResult, SecretSource, classify_cli_error, coerce_float,
    get_source_environment, is_valid_env_name, run_cli,
)

logger = logging.getLogger(__name__)

_OP_RUN_TIMEOUT = 30

# `op` itself reads OP_SERVICE_ACCOUNT_TOKEN; `service_account_token_env` lets
# the user source it from another name, and _op_child_env normalizes it back.
_DEFAULT_TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"

# Minimal allowlisted child env (never the full post-dotenv os.environ, which
# holds every provider credential). OP_SESSION_* and the token are added
# dynamically in _op_child_env().
_OP_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemRoot",
    "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "OP_CONFIG_DIR",
    "OP_ACCOUNT", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN",
    # Lets a user skip op's desktop-app integration probe (which can hang with
    # no timeout on a wedged desktop container) and go straight to token auth.
    "OP_LOAD_DESKTOP_APP_SETTINGS",
)

# L1 key folds in str(home_path) so a HERMES_HOME switch inside one long-lived
# process (the gateway) can't return another profile's secrets. The disk key
# omits home because the file already lives under <home>/cache/.
_CacheKey = Tuple[str, str, str, str]  # (auth_fp, account, home, refs_fp)
_DISK_CACHE_BASENAME = "op_cache.json"


def _disk_key_str(cache_key: _CacheKey) -> str:
    auth_fp, account, _home, refs_fp = cache_key
    return f"{auth_fp}|{account}|{refs_fp}"


_STORE: SecretCache[_CacheKey] = SecretCache(_DISK_CACHE_BASENAME, key_serializer=_disk_key_str)
_CACHE = _STORE.memory  # tests flush L1 directly

# Rate-limit backoff marker: {"auth": <auth fingerprint>, "until": <epoch>}. Holds no
# secret values, so it is written even when cache_ttl_seconds is 0.
_BACKOFF_BASENAME = "op_rate_limit.json"
# Used when op's message carries no retry hint. 1Password's hourly window is 60 min.
_DEFAULT_BACKOFF_SECONDS = 15 * 60
_MAX_BACKOFF_SECONDS = 24 * 60 * 60
_RETRY_HINT_RE = re.compile(
    r"(?:try again|retry)\s+(?:in\s+)?(?:about\s+|approximately\s+)?"
    r"(?:(\d+)\s*hours?)?(?:\s*(?:and|,)?\s*(\d+)\s*minutes?)?", re.IGNORECASE)

_MISSING_BINARY_HINT = (
    "Install the 1Password CLI (https://developer.1password.com/docs/cli/get-started/) "
    "or set secrets.onepassword.binary_path."
)

# First matching rule wins. RATE_LIMITED leads: op's 429 text must never fall
# through to a kind whose policy is "retry".
_OP_ERROR_RULES = (
    (ErrorKind.RATE_LIMITED, ("too many requests", "rate-limit", "rate limit", "429")),
    (ErrorKind.TIMEOUT, ("timed out",)),
    (ErrorKind.BINARY_MISSING, ("not found on path", "not an executable", "failed to invoke")),
    (ErrorKind.AUTH_FAILED, ("unauthorized", "not signed in", "session expired",
                             "authentication", "401", "403")),
    (ErrorKind.EMPTY_VALUE, ("empty value",)),
    (ErrorKind.NETWORK, ("network", "connection", "resolve host", "dns")),
)


def _classify_op_error(message: str) -> ErrorKind:
    return classify_cli_error(message, _OP_ERROR_RULES)


def _validate_references(references: Optional[Dict[str, str]]) -> Tuple[Dict[str, str], List[str]]:
    """``(valid_refs, warnings)``: keep valid env names bound to stripped ``op://`` strings."""
    valid: Dict[str, str] = {}
    warnings: List[str] = []
    for name, ref in (references or {}).items():
        if not is_valid_env_name(name):
            warnings.append(f"Skipping {name!r}: not a valid env-var name")
        elif not isinstance(ref, str):
            warnings.append(f"Skipping {name!r}: reference is not a string")
        elif not ref.strip().startswith("op://"):
            warnings.append(f"Skipping {name!r}: {ref!r} is not an op:// secret reference")
        else:
            valid[name] = ref.strip()
    return valid, warnings


def _auth_fingerprint(token_env: str) -> str:
    """SHA-256 prefix over everything `op` would authenticate with (token, account,
    Connect host/token, ``OP_SESSION_*``), so a new identity never sees old cached values."""
    source_env = get_source_environment()
    parts: List[str] = [f"{label}={source_env.get(var, '')}" for label, var in (
        ("token", token_env), ("account", "OP_ACCOUNT"),
        ("connect_host", "OP_CONNECT_HOST"), ("connect_token", "OP_CONNECT_TOKEN"))]
    parts += [f"{key}={source_env[key]}" for key in sorted(source_env) if key.startswith("OP_SESSION_")]
    return _fingerprint("\n".join(parts))


def _refs_fingerprint(references: Dict[str, str]) -> str:
    return _fingerprint("\n".join(f"{name}={references[name]}" for name in sorted(references)))


def find_op(binary_path: str = "") -> Optional[Path]:
    """Resolve a usable ``op`` binary, or None. A pinned ``binary_path`` is used
    verbatim — pinned-but-missing returns None rather than falling back to PATH."""
    found = binary_path or shutil.which("op")
    if not found or (binary_path and not os.access(binary_path, os.X_OK)):
        return None
    return Path(found)


def _scrub(text: str) -> str:
    """Full ECMA-48 ANSI strip (so a control sequence can't hide text after a redaction marker) + trim."""
    from tools.ansi_strip import strip_ansi

    return strip_ansi(text).replace("\x1b", "").strip()


def _op_child_env(token_value: str) -> Dict[str, str]:
    source_env = get_source_environment()
    env = {k: source_env[k] for k in _OP_ENV_ALLOWLIST if k in source_env}
    env.update((k, v) for k, v in source_env.items() if k.startswith("OP_SESSION_"))
    if token_value:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token_value
    env["NO_COLOR"] = "1"
    return env


def _run_op(op: Path, args: List[str], target: str, *, account: str, token_value: str, what: str) -> str:
    """Run ``op <args…> [--account A] -- <target>``; stdout on success, ``RuntimeError`` otherwise.
    ``--`` so a reference or item name can never parse as an op flag."""
    cmd: List[str] = [str(op), *args]
    if account:
        cmd += ["--account", account]
    cmd += ["--", target]

    proc = run_cli(cmd, env=_op_child_env(token_value), timeout=_OP_RUN_TIMEOUT, label="op",
                   timeout_message=f"op {what} timed out after {_OP_RUN_TIMEOUT}s for {target!r}", stdin=None)

    if proc.returncode != 0:
        # Room for op's full 429 text including its "try again in …" hint.
        err = _scrub(proc.stderr or "")[:300]
        if err:
            raise RuntimeError(f"op {what} failed for {target!r}: {err}")
        raise RuntimeError(f"op {what} exited {proc.returncode} for {target!r}")
    return proc.stdout or ""


def _run_op_read(op: Path, reference: str, *, account: str = "", token_value: str = "") -> str:
    """Resolve one ``op://`` reference; raises ``RuntimeError`` on any failure, including
    an exit-0 empty value (applying it would clobber a good credential with ``""``)."""
    # Strip only op's trailing newline so intentional edge spaces survive.
    value = _run_op(op, ["read"], reference, account=account, token_value=token_value,
                    what="read").rstrip("\r\n")
    if not value.strip():
        raise RuntimeError(f"op read returned an empty value for {reference!r}")
    return value


def _split_reference(reference: str) -> Optional[Tuple[str, str, Tuple[str, ...]]]:
    """``op://vault/item/[section/]field`` → ``(vault, item, field_path)``; None for shapes the
    item-JSON resolver does not handle (query attributes like ``?attribute=otp``, odd depth)."""
    if "?" in reference:
        return None
    parts = reference[len("op://"):].split("/")
    if len(parts) not in (3, 4) or not all(parts):
        return None
    return parts[0], parts[1], tuple(parts[2:])


def _field_from_item(item: dict, field_path: Tuple[str, ...]) -> Optional[str]:
    """The value ``op read`` would return for ``field_path``, or None when the item JSON can't say
    unambiguously (no match, several matches, no value) — the caller then falls back to ``op read``."""
    want = field_path[-1]
    section = field_path[0] if len(field_path) == 2 else None
    matches = []
    for fld in item.get("fields") or []:
        if not isinstance(fld, dict) or want not in (fld.get("label"), fld.get("id")):
            continue
        if section is not None:
            sec = fld.get("section") if isinstance(fld.get("section"), dict) else {}
            if section not in (sec.get("label"), sec.get("id")):
                continue
        matches.append(fld)
    if len(matches) != 1:
        return None
    value = matches[0].get("value")
    return value if isinstance(value, str) and value.strip() else None


def _item_groups(refs: Dict[str, str]) -> Tuple[Dict[Tuple[str, str], Dict[str, Tuple[str, ...]]], Dict[str, str]]:
    """Split ``refs`` into ``{(vault, item): {name: field_path}}`` for items referenced 2+ times
    (one ``op item get`` each) and the remainder resolved one ``op read`` apiece."""
    by_item: Dict[Tuple[str, str], Dict[str, Tuple[str, ...]]] = {}
    singles: Dict[str, str] = {}
    for name, ref in refs.items():
        split = _split_reference(ref)
        if split is None:
            singles[name] = ref
        else:
            by_item.setdefault((split[0], split[1]), {})[name] = split[2]
    grouped = {key: names for key, names in by_item.items() if len(names) > 1}
    for key, names in by_item.items():
        if len(names) == 1:
            (name,) = names
            singles[name] = refs[name]
    return grouped, singles


def _is_rate_limited(exc: Exception) -> bool:
    return _classify_op_error(str(exc)) is ErrorKind.RATE_LIMITED


def _backoff_seconds(message: str) -> float:
    """Seconds op asked us to wait (``… try again in 23 hours and 59 minutes``), else the default."""
    match = _RETRY_HINT_RE.search(message)
    if match and (match.group(1) or match.group(2)):
        seconds = int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60
        if seconds > 0:
            return float(min(seconds, _MAX_BACKOFF_SECONDS))
    return float(_DEFAULT_BACKOFF_SECONDS)


def _backoff_path(home_path: Optional[Path]) -> Path:
    return resolve_cache_home(home_path) / "cache" / _BACKOFF_BASENAME


def _active_backoff(auth_fp: str, home_path: Optional[Path]) -> Optional[float]:
    """Epoch until which ``op`` must not be called for this identity, or None."""
    try:
        with open(_backoff_path(home_path), "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("auth") != auth_fp:
        return None
    until = payload.get("until")
    if not isinstance(until, (int, float)) or until <= time.time():
        return None
    return float(until)


def _record_backoff(auth_fp: str, message: str, home_path: Optional[Path]) -> float:
    until = time.time() + _backoff_seconds(message)
    try:
        atomic_write_json(_backoff_path(home_path), {"auth": auth_fp, "until": until})
    except OSError:
        pass  # best-effort — without the marker the next process just pays one more request
    return until


def _clear_backoff(home_path: Optional[Path]) -> None:
    try:
        _backoff_path(home_path).unlink()
    except (FileNotFoundError, OSError):
        pass


def _fmt_epoch(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(epoch))


def fetch_onepassword_secrets(
    *, references: Dict[str, str], account: str = "", token_env: str = _DEFAULT_TOKEN_ENV,
    binary: Optional[Path] = None, binary_path: str = "", use_cache: bool = True,
    cache_ttl_seconds: float = 300, home_path: Optional[Path] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """Resolve ``references`` (name → ``op://…``) to ``(secrets, warnings)``.

    Raises ``RuntimeError`` only when no ``op`` binary is available; per-ref
    failures become warnings. Only a complete, error-free pull is cached, so a
    transient auth failure isn't frozen in for the whole TTL window.

    With ``use_cache``: an active rate-limit backoff skips ``op`` entirely, and
    names lost to a rate limit, network error or timeout are filled from the
    last complete pull (any age; never after an auth failure, where old values
    would mask a real problem).
    """
    valid, warnings = _validate_references(references)
    if not valid:
        return {}, warnings

    token_value = get_source_environment().get(token_env, "").strip()
    auth_fp = _auth_fingerprint(token_env)
    cache_key: _CacheKey = (auth_fp, account or "",
                            str(home_path) if home_path is not None else "", _refs_fingerprint(valid))

    if use_cache:
        cached = _STORE.lookup(cache_key, cache_ttl_seconds, home_path)
        if cached is not None:
            return dict(cached.secrets), warnings

    def _fill_from_last_good(secrets: Dict[str, str], names: List[str], reason: str) -> None:
        if not (use_cache and cache_ttl_seconds > 0) or not names:
            return
        last = _STORE.memory.get(cache_key) or _STORE.disk.read(cache_key, float("inf"), home_path)
        filled = [n for n in names if last is not None and n in last.secrets]
        for name in filled:
            secrets[name] = last.secrets[name]
        if filled:
            age = int(max(0.0, time.time() - last.fetched_at))
            warnings.append(f"{reason}; served {len(filled)} value(s) from the last complete pull ({age}s old)")

    secrets: Dict[str, str] = {}
    if use_cache:
        until = _active_backoff(auth_fp, home_path)
        if until is not None:
            warnings.append(f"1Password rate-limited; not calling op until {_fmt_epoch(until)}")
            _fill_from_last_good(secrets, sorted(valid), "1Password backoff active")
            return secrets, warnings

    op = binary or find_op(binary_path)
    if op is None:
        raise RuntimeError("op CLI not found.  Install the 1Password CLI "
                           "(https://developer.1password.com/docs/cli/get-started/) or set "
                           "secrets.onepassword.binary_path to its absolute location.")

    read_errors = 0
    transient_failed: List[str] = []
    rate_limit_msg: Optional[str] = None

    grouped, pending_reads = _item_groups(valid)
    for (vault, item_name), fields in sorted(grouped.items()):
        item: Optional[dict] = None
        try:
            parsed = json.loads(_run_op(op, ["item", "get", "--vault", vault, "--format", "json"], item_name,
                                        account=account, token_value=token_value, what="item get"))
            item = parsed if isinstance(parsed, dict) else None
        except RuntimeError as exc:
            if _is_rate_limited(exc):
                rate_limit_msg = str(exc)
                break
            if _classify_op_error(str(exc)) in (ErrorKind.AUTH_FAILED, ErrorKind.AUTH_EXPIRED):
                # Every field read would fail the same way; don't spend a request on each.
                warnings.append(str(exc))
                read_errors += len(fields)
                continue
            # Otherwise (item renamed, field-level quirk) the per-field `op read` below
            # reports the real per-ref error.
        except ValueError:  # malformed JSON — fall back to op read
            pass
        for name, field_path in fields.items():
            value = _field_from_item(item, field_path) if item is not None else None
            if value is None:
                pending_reads[name] = valid[name]
            else:
                secrets[name] = value

    if rate_limit_msg is None:
        for name in sorted(pending_reads):
            try:
                secrets[name] = _run_op_read(op, pending_reads[name], account=account, token_value=token_value)
            except RuntimeError as exc:
                if _is_rate_limited(exc):
                    rate_limit_msg = str(exc)
                    break
                warnings.append(str(exc))
                read_errors += 1
                if _classify_op_error(str(exc)) in (ErrorKind.NETWORK, ErrorKind.TIMEOUT):
                    transient_failed.append(name)

    complete = rate_limit_msg is None and not read_errors
    if rate_limit_msg is not None:
        missing = sorted(n for n in valid if n not in secrets)
        until = _record_backoff(auth_fp, rate_limit_msg, home_path)
        warnings.append(f"{rate_limit_msg} — stopped at the first throttled call; {len(missing)} "
                        f"reference(s) not fetched; op skipped until {_fmt_epoch(until)}")
        _fill_from_last_good(secrets, missing, "1Password rate-limited")
    elif transient_failed:
        _fill_from_last_good(secrets, transient_failed, "1Password unreachable")

    if use_cache and complete and secrets:
        _STORE.store(cache_key, CachedFetch(secrets=dict(secrets), fetched_at=time.time()),
                     cache_ttl_seconds, home_path)

    return secrets, warnings


def _missing_binary_error(binary_path: str) -> str:
    if binary_path:
        return f"secrets.onepassword.binary_path ({binary_path!r}) is not an executable op binary."
    return ("secrets.onepassword.enabled is true but the op CLI was not found on PATH.  Install it "
            "(https://developer.1password.com/docs/cli/get-started/) or set secrets.onepassword.binary_path.")


def apply_onepassword_secrets(
    *, enabled: bool, env: Optional[Dict[str, str]] = None, account: str = "",
    service_account_token_env: str = _DEFAULT_TOKEN_ENV, binary_path: str = "",
    override_existing: bool = True, cache_ttl_seconds: float = 300, home_path: Optional[Path] = None,
) -> FetchResult:
    """Resolve configured ``op://`` references and set them on ``os.environ``
    (``hermes secrets onepassword sync --apply``). Never raises. Refs already
    satisfied by the env (when ``override_existing`` is false) and the token var
    are skipped *before* fetching, so ``op`` never runs for a discarded value."""
    result = FetchResult()
    if not enabled:
        return result

    valid, warnings = _validate_references(env)
    result.warnings.extend(warnings)

    def _guarded(name: str) -> bool:
        """True when ``name`` must not be applied (token var or env already set)."""
        return name == service_account_token_env or (not override_existing and bool(os.environ.get(name)))

    result.skipped.extend(n for n in valid if _guarded(n))
    refs_to_fetch = {n: ref for n, ref in valid.items() if not _guarded(n)}
    if not refs_to_fetch:
        return result

    binary = find_op(binary_path)
    result.binary_path = binary
    if binary is None:
        result.error = _missing_binary_error(binary_path)
        return result

    try:
        secrets, fetch_warnings = fetch_onepassword_secrets(
            references=refs_to_fetch, account=account, token_env=service_account_token_env,
            binary=binary, cache_ttl_seconds=cache_ttl_seconds, home_path=home_path)
    except RuntimeError as exc:
        result.error = str(exc)
        return result

    result.secrets = secrets
    result.warnings.extend(fetch_warnings)
    for name, value in secrets.items():
        if _guarded(name):  # defensive re-check: keys should already be ⊆ refs_to_fetch
            if name not in result.skipped:
                result.skipped.append(name)
            continue
        os.environ[name] = value
        result.applied.append(name)
    return result


class OnePasswordSource(SecretSource):
    """1Password as a registered **mapped** source (explicit per-var bindings, so
    its claims outrank bulk sources on contested vars)."""

    name = "onepassword"
    label = "1Password"
    shape = "mapped"
    scheme = "op"
    token_env_key = "service_account_token_env"
    default_token_env = _DEFAULT_TOKEN_ENV
    # override_existing defaults True: an explicit VAR→op:// binding is the
    # strongest user intent; a stale .env line must not silently defeat it.
    override_existing_default = True
    _AUTH_HINT = ("Run `hermes secrets onepassword token` to paste a fresh service-account token "
                  "({token_env}), or `op signin` for an interactive session.")
    remediation_hints = {ErrorKind.AUTH_FAILED: _AUTH_HINT, ErrorKind.AUTH_EXPIRED: _AUTH_HINT,
                         ErrorKind.BINARY_MISSING: _MISSING_BINARY_HINT}

    def config_schema(self) -> dict:
        return {
            "enabled": {"description": "Master switch", "default": False},
            "env": {"description": "Map of ENV_VAR -> op://vault/item/field reference", "default": {}},
            "account": {"description": "op --account shorthand (empty = default account)", "default": ""},
            "service_account_token_env": {"description": "Env var holding the service-account token "
                                                         "(unset = desktop/interactive session)",
                                          "default": _DEFAULT_TOKEN_ENV},
            "binary_path": {"description": "Pin the op binary (empty = resolve via PATH)", "default": ""},
            "cache_ttl_seconds": {"description": "Disk+memory cache TTL; 0 disables", "default": 300},
            "override_existing": {"description": "Resolved values overwrite .env/shell values", "default": True},
        }

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        cfg = cfg if isinstance(cfg, dict) else {}
        result = FetchResult()

        env_map = cfg.get("env")
        valid, warnings = _validate_references(env_map if isinstance(env_map, dict) else None)
        result.warnings.extend(warnings)
        if not valid:
            if not warnings:
                result.fail("secrets.onepassword.enabled is true but the env: map is "
                            "empty.  Add ENV_VAR: op://vault/item/field entries.", ErrorKind.NOT_CONFIGURED)
            return result

        binary_path = str(cfg.get("binary_path") or "")
        binary = find_op(binary_path)
        result.binary_path = binary
        if binary is None:
            return result.fail(_missing_binary_error(binary_path), ErrorKind.BINARY_MISSING)

        try:
            secrets, fetch_warnings = fetch_onepassword_secrets(
                references=valid, account=str(cfg.get("account") or ""), token_env=self.token_env(cfg),
                binary=binary, cache_ttl_seconds=coerce_float(cfg.get("cache_ttl_seconds", 300), 300.0),
                home_path=home_path)
        except RuntimeError as exc:
            return result.fail(str(exc), _classify_op_error(str(exc)))

        result.secrets = secrets
        result.warnings.extend(fetch_warnings)
        return result


def clear_caches(home_path: Optional[Path] = None) -> None:
    """Drop in-process AND disk caches (after a token rotation, so the next
    startup resolves fresh instead of serving values cached under the old token).
    Also drops the rate-limit backoff marker."""
    _STORE.clear(home_path)
    _clear_backoff(home_path)


_reset_cache_for_tests = clear_caches


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import hashlib  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'DiskCache': ('agent.secret_sources._cache', 'DiskCache'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
