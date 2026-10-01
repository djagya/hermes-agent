"""Managed browser-control client (HER-130).

When ``HERMES_BROWSER_CONTROL_URL`` or ``browser.control_url`` is set,
every browser entry point must acquire a gated CDP URL from monolith
browser-control. Ambient ``BU_CDP_*``, ``BROWSER_CDP_URL`` and
``browser.cdp_url`` cannot win. Cache keys include slot, browser
generation and ownership epoch.

This module is stdlib-only and must stay aligned with
``scripts/pylib/monolith/browser_route.py`` in djagya/monolith.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MANAGED_CONTROL_ENV = "HERMES_BROWSER_CONTROL_URL"
MANAGED_KEY_ENV = "HERMES_BROWSER_CONTROL_KEY"
MANAGED_LEASE_ENV = "HERMES_BROWSER_LEASE_ID"
MANAGED_TOKEN_ENV = "HERMES_BROWSER_COMMAND_TOKEN"
# Sentinel returned by control_url() when config.yaml cannot be read: keeps
# the install in managed mode (no local/cloud Chrome) with a clear error.
CONFIG_UNREADABLE_URL = "managed://config-unreadable"
# Operator rollback: managed mode stays ON; every entry point fails closed.
DISABLED_CONTROL_URL = "disabled://"
OPERATOR_DISABLED_ERROR = "unavailable: browsing disabled by operator"

# Acquire can wait on a Chrome boot behind the control lock; 5 s used to
# time out a request the server then completed, orphaning the lease.
CONTROL_TIMEOUT_S = 60.0
STATUS_TIMEOUT_S = 10.0

_held_lock = threading.Lock()
_held_leases: Dict[str, Dict[str, str]] = {}


class ManagedBrowserError(Exception):
    """Structured managed-mode failure. ``str()`` is ``code: detail``."""


def daemon_cache_key(slot_id: str, browser_generation: str, ownership_epoch: int) -> str:
    return f"bu-ctrl-{slot_id}-{browser_generation}-{ownership_epoch}"


def control_url() -> str:
    """Env wins over ``browser.control_url`` in config.yaml."""
    env_url = (os.environ.get(MANAGED_CONTROL_ENV) or "").strip()
    if env_url:
        return env_url
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
    except (ImportError, OSError, ValueError, TypeError):
        # Unreadable config must not silently mean "unmanaged": that would
        # let a local Chrome spawn. Report managed with no reachable URL.
        return CONFIG_UNREADABLE_URL
    section = cfg.get("browser", {}) if isinstance(cfg, dict) else {}
    if isinstance(section, dict):
        return str(section.get("control_url") or "").strip()
    return ""


def is_disabled_url(url: str) -> bool:
    text = (url or "").strip().lower()
    return text == DISABLED_CONTROL_URL or text.startswith("disabled:")


def is_managed() -> bool:
    return bool(control_url())


def control_key() -> str:
    return (os.environ.get(MANAGED_KEY_ENV) or "").strip()


def clear_held_leases() -> None:
    with _held_lock:
        _held_leases.clear()


def _hold_key(
    task_id: Optional[str], session_name: str, identity: str = "", mode: str = "research"
) -> str:
    """One held lease per (task, identity, mode).

    Keying by session name alone handed a lease minted for one identity to
    a later call asking for another (a ``sensitive`` call ran in a
    temporary research slot) and reused one cron run's lease name in the
    next run. The browser a lease points at is chosen by control, not by
    the session name, so the name is only a fallback when there is no task.
    """
    owner = (str(task_id or "").strip() or (session_name or "").strip() or "default")
    return f"{owner}|{identity}|{mode}"


def _coerce_mode(mode: Optional[str]) -> str:
    value = str(mode or "research").strip().lower()
    return value if value in {"research", "qa"} else "research"


def _coerce_targets(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _ambient_cdp_override() -> str:
    env_override = (os.environ.get("BROWSER_CDP_URL") or "").strip()
    if env_override:
        return env_override
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        section = cfg.get("browser", {}) if isinstance(cfg, dict) else {}
        if isinstance(section, dict):
            return str(section.get("cdp_url", "") or "").strip()
    except Exception:
        return ""
    return ""


def _refuse_overrides(env: Dict[str, str], raw_cdp_override: str = "") -> Optional[str]:
    if env.get("BU_CDP_WS") or env.get("BU_CDP_URL"):
        return "scope_denied: managed mode refuses BU_CDP_* overrides"
    if (os.environ.get("BROWSER_CDP_URL") or "").strip() or (raw_cdp_override or "").strip():
        return "scope_denied: managed mode refuses BROWSER_CDP_URL / browser.cdp_url overrides"
    spawn = (os.environ.get("BU_AUTOSPAWN") or "").strip().lower()
    if spawn in {"1", "true", "yes"}:
        return "scope_denied: managed mode refuses BU_AUTOSPAWN cloud fallback"
    return None


def _apply_lease(env: Dict[str, str], lease: Dict[str, Any], control: str) -> Optional[str]:
    slot_id = str(lease.get("slot_id") or "")
    generation = str(lease.get("browser_generation") or "")
    epoch = int(lease.get("ownership_epoch") or 0)
    token = str(lease.get("command_token") or "")
    if not (slot_id and generation and token):
        return "unavailable: control acquire omitted slot/generation/token"
    env["BU_CDP_URL"] = f"{control.rstrip('/')}/slot/{slot_id}/json/version?tok={token}"
    env[MANAGED_LEASE_ENV] = str(lease.get("lease_id") or "")
    env[MANAGED_TOKEN_ENV] = token
    env["BU_NAME"] = daemon_cache_key(slot_id, generation, epoch)
    env["_HERMES_BU_PRIVATE_BROWSER"] = "1"
    return None


def _format_error(lease: Dict[str, Any]) -> str:
    code = str(lease.get("error") or "unavailable")
    detail = str(lease.get("detail") or "").strip()
    if code == "busy":
        extra = detail or "browser slot queued"
        return (
            f"busy: {extra} — retry this call or cancel; "
            "do not use a local, cloud, or CDP fallback"
        )
    if detail:
        return f"{code}: {detail}"
    return code


def _post(control: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = control_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = Request(
        control.rstrip("/") + path,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(req, timeout=CONTROL_TIMEOUT_S) as resp:
            raw = resp.read()
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"error": "unavailable", "detail": f"control HTTP {exc.code}"}
        if isinstance(data, dict):
            return data
        return {"error": "unavailable", "detail": "control returned a non-object"}
    except URLError as exc:
        return {"error": "unavailable", "detail": f"control unreachable: {exc.reason}"}
    except OSError as exc:
        # A read timeout is a bare TimeoutError, not URLError.
        return {"error": "unavailable", "detail": f"control unreachable: {exc}"}
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return {"error": "unavailable", "detail": "control returned non-JSON"}
    return data if isinstance(data, dict) else {"error": "unavailable"}


def _lease_alive(control: str, lease_id: str) -> Optional[bool]:
    """True/False from ``GET /v1/status?lease=``; None when control did not answer.

    A cached lease dies without this process noticing: idle recycle, an
    in-code ``/v1/release``, a human take (and Return to Sera, which mints
    a new lease), a controller restart. Reusing it made every later call
    fail at the gate (surfacing as the harness's "permission-blocked").
    """
    if not lease_id:
        return False
    headers = {}
    key = control_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = Request(
        control.rstrip("/") + "/v1/status?" + urlencode({"lease": lease_id}),
        method="GET",
        headers=headers,
    )
    try:
        with urlopen(req, timeout=STATUS_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            return None
        return False if isinstance(data, dict) and data.get("error") == "stale" else None
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("error"):
        return False if isinstance(data, dict) and data.get("error") == "stale" else None
    return data.get("owner") == "agent" and data.get("state") == "agent"


def _forget_held(key: str) -> None:
    with _held_lock:
        _held_leases.pop(key, None)


def _acquire(
    control: str,
    *,
    task: str,
    mode: str,
    identity: str,
    qa_targets: List[str],
) -> Dict[str, Any]:
    if control == CONFIG_UNREADABLE_URL:
        return {
            "error": "unavailable",
            "detail": "config.yaml unreadable; refusing unmanaged browser fallback",
        }
    if is_disabled_url(control):
        return {"error": "unavailable", "detail": "browsing disabled by operator"}
    if not control_key():
        return {"error": "unavailable", "detail": f"{MANAGED_KEY_ENV} missing"}
    return _post(
        control,
        "/v1/acquire",
        {
            "task": task or "browser-exec",
            "mode": mode,
            "identity": identity,
            "qa_targets": qa_targets,
        },
    )


def resolve_managed_cdp(
    env: Dict[str, str],
    task_id: Optional[str] = "",
    session_name: str = "",
    mode: str = "",
    identity: str = "",
    qa_targets: Any = None,
    raw_cdp_override: Optional[str] = None,
    reconnect: bool = False,
) -> Optional[str]:
    """Return an error string, or None after pointing ``env`` at the gate.

    Callers must invoke this only when :func:`is_managed` is true so a
    successful None does not fall through to local/cloud backends.

    A ``stale`` acquire (reconnect with an old generation/epoch) is retried
    once. ``busy`` is a structured queued error: retry or cancel, never
    fall back.
    """
    control = control_url()
    if not control:
        return "unavailable: managed control URL missing"
    if is_disabled_url(control):
        return OPERATOR_DISABLED_ERROR

    if raw_cdp_override is None:
        raw_cdp_override = _ambient_cdp_override()
    refused = _refuse_overrides(env, raw_cdp_override)
    if refused:
        return refused

    payload_mode = _coerce_mode(mode)
    payload_identity = str(identity or "")
    key = _hold_key(task_id, session_name, payload_identity, payload_mode)
    with _held_lock:
        cached = None if reconnect else _held_leases.get(key)
    if cached:
        alive = _lease_alive(control, cached.get(MANAGED_LEASE_ENV, ""))
        if alive is None:
            return "unavailable: control unreachable while checking the held lease"
        if alive:
            env.update(cached)
            return None
        # Dead lease: forget it and acquire again (control hands a task its
        # own resumed lease back, or a fresh slot).
        _forget_held(key)

    payload_targets = _coerce_targets(qa_targets)
    task = str(task_id or session_name or "browser-exec")

    lease = _acquire(
        control,
        task=task,
        mode=payload_mode,
        identity=payload_identity,
        qa_targets=payload_targets,
    )
    if lease.get("error") == "stale":
        lease = _acquire(
            control,
            task=task,
            mode=payload_mode,
            identity=payload_identity,
            qa_targets=payload_targets,
        )
    if lease.get("error"):
        return _format_error(lease)

    applied = _apply_lease(env, lease, control)
    if applied:
        return applied
    snapshot = {
        "BU_CDP_URL": env["BU_CDP_URL"],
        MANAGED_LEASE_ENV: env.get(MANAGED_LEASE_ENV, ""),
        MANAGED_TOKEN_ENV: env.get(MANAGED_TOKEN_ENV, ""),
        "BU_NAME": env.get("BU_NAME", ""),
        "_HERMES_BU_PRIVATE_BROWSER": "1",
    }
    with _held_lock:
        _held_leases[key] = snapshot
    return None


def managed_cdp_or_error(
    env: Optional[Dict[str, str]] = None,
    *,
    task_id: str = "",
    session_name: str = "",
    mode: str = "",
    identity: str = "",
    qa_targets: Any = None,
    raw_cdp_override: Optional[str] = None,
    reconnect: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Single choke-point helper for built-in and CLI paths.

    Returns ``(None, None)`` when unmanaged, ``(cdp_url, None)`` on
    managed success, or ``(None, error)`` on managed failure.
    """
    if not is_managed():
        return None, None
    hold = env if env is not None else {}
    err = resolve_managed_cdp(
        hold,
        task_id=task_id,
        session_name=session_name,
        mode=mode,
        identity=identity,
        qa_targets=qa_targets,
        raw_cdp_override=raw_cdp_override,
        reconnect=reconnect,
    )
    if err:
        return None, err
    return hold.get("BU_CDP_URL") or None, None


def release_lease(env: Dict[str, str]) -> Optional[str]:
    """Best-effort ``POST /v1/release`` with lease id + command token."""
    control = control_url()
    lease_id = str(env.get(MANAGED_LEASE_ENV) or "").strip()
    token = str(env.get(MANAGED_TOKEN_ENV) or "").strip()
    if not control or not lease_id:
        return None
    if is_disabled_url(control):
        return OPERATOR_DISABLED_ERROR
    cache_name = str(env.get("BU_NAME") or "").strip()
    with _held_lock:
        for key, held in list(_held_leases.items()):
            if held.get(MANAGED_LEASE_ENV) == lease_id or (
                cache_name and held.get("BU_NAME") == cache_name
            ):
                _held_leases.pop(key, None)
    if not control_key():
        return f"unavailable: {MANAGED_KEY_ENV} missing"
    result = _post(
        control,
        "/v1/release",
        {"lease_id": lease_id, "token": token},
    )
    if result.get("error"):
        return _format_error(result)
    return None


def release_task_leases(owners: Any) -> int:
    """Release every lease held for these task ids; returns how many.

    The gateway is long-lived, so atexit never fires and a lease held by a
    finished session, a /new or /reset, or a completed cron run used to
    stay with control until its idle sweep, starving the slot pool. Call
    from the agent's close path, off the event loop (blocking HTTP).
    """
    wanted = {str(owner).strip() for owner in (owners or ()) if str(owner or "").strip()}
    if not wanted:
        return 0
    with _held_lock:
        keys = [key for key in _held_leases if key.split("|", 1)[0] in wanted]
        snapshots = [_held_leases.pop(key) for key in keys]
    for snapshot in snapshots:
        try:
            release_lease(snapshot)
        except Exception:
            pass
    return len(snapshots)


def release_all_held_leases() -> None:
    with _held_lock:
        held = list(_held_leases.values())
        _held_leases.clear()
    for snapshot in held:
        try:
            release_lease(snapshot)
        except Exception:
            pass


atexit.register(release_all_held_leases)
