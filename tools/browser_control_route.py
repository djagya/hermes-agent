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
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MANAGED_CONTROL_ENV = "HERMES_BROWSER_CONTROL_URL"
MANAGED_KEY_ENV = "HERMES_BROWSER_CONTROL_KEY"
MANAGED_LEASE_ENV = "HERMES_BROWSER_LEASE_ID"
MANAGED_TOKEN_ENV = "HERMES_BROWSER_COMMAND_TOKEN"
# The slot and Chrome generation control granted; the post-card-fill capture
# guard keys on them (tools/browser_payment_privacy.py).
MANAGED_SLOT_ENV = "HERMES_BROWSER_SLOT_ID"
MANAGED_GENERATION_ENV = "HERMES_BROWSER_GENERATION"
# Control's wipe counter for the slot at acquire time, and the slot's profile
# kind. The capture guard records the counter with a card-fill mark and
# clears the mark once control reports a later wipe of a temporary profile.
MANAGED_WIPE_ENV = "HERMES_BROWSER_WIPE_COUNT"
MANAGED_PROFILE_KIND_ENV = "HERMES_BROWSER_PROFILE_KIND"
# The controller key may come from a file instead of the environment.
MANAGED_KEY_FILE_ENV = "HERMES_BROWSER_CONTROL_KEY_FILE"
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
# A queued acquire waits in control's fair order this long by default
# (callers pass a smaller budget from their own timeout). Each retry
# honours control's ``retry_after_s``, clamped to this range.
BUSY_WAIT_ENV = "HERMES_BROWSER_BUSY_WAIT_S"
DEFAULT_BUSY_WAIT_S = 90.0
RETRY_AFTER_MIN_S = 1.0
RETRY_AFTER_MAX_S = 30.0
DEFAULT_RETRY_AFTER_S = 5.0

_held_lock = threading.Lock()
_held_leases: dict[str, dict[str, str]] = {}
# Hold key -> [lock, users]. Guarded by ``_held_lock``; an entry lives only
# while a caller is inside :func:`_single_flight` for that key.
_key_locks: dict[str, list[Any]] = {}


class ManagedBrowserError(Exception):
    """Structured managed-mode failure. ``str()`` is ``code: detail``."""


class RouteError(str):
    """An error string that also carries structured fields for the tool result.

    ``resolve_managed_cdp`` has always returned a plain error string; a
    ``busy`` result now also carries the control queue ticket, position and
    retry hint as :attr:`fields`, which callers may merge into their JSON.
    """

    fields: dict[str, Any]

    def __new__(cls, text: str, fields: Optional[dict[str, Any]] = None) -> RouteError:
        obj = super().__new__(cls, text)
        obj.fields = dict(fields or {})
        return obj


# Seams for tests (a bounded wait must not really sleep in unit tests).
_sleep = time.sleep
_monotonic = time.monotonic


def daemon_cache_key(slot_id: str, browser_generation: str, ownership_epoch: int) -> str:
    return f"bu-ctrl-{slot_id}-{browser_generation}-{ownership_epoch}"


def _retire_daemon(name: str) -> None:
    """Stop the harness daemon of a lease this process let go (released, or found dead).

    The daemon is named per lease and never exits when control fences its CDP
    socket, so every recycled lease left one behind (21 in the gateway on
    2026-10-26). Off-thread: ``--reload`` can take seconds.
    """
    if not str(name or "").startswith("bu-ctrl-"):
        return
    try:
        from tools.browser_use_cli import stop_harness_daemon
    except ImportError:
        return
    threading.Thread(target=stop_harness_daemon, args=(name,), name="bu-daemon-retire", daemon=True).start()


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
    """Bearer key for control. ``HERMES_BROWSER_CONTROL_KEY_FILE`` wins (an
    unreadable file means no key, never a fallback to a retired env value),
    else ``HERMES_BROWSER_CONTROL_KEY``; the same rule browser-control uses.
    Never returned to the model."""
    path = (os.environ.get(MANAGED_KEY_FILE_ENV) or "").strip()
    if path:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                return fh.read().strip()
        except OSError:
            return ""
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


def _coerce_targets(raw: Any) -> list[str]:
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


def _refuse_overrides(env: dict[str, str], raw_cdp_override: str = "") -> Optional[str]:
    if env.get("BU_CDP_WS") or env.get("BU_CDP_URL"):
        return "scope_denied: managed mode refuses BU_CDP_* overrides"
    if (os.environ.get("BROWSER_CDP_URL") or "").strip() or (raw_cdp_override or "").strip():
        return "scope_denied: managed mode refuses BROWSER_CDP_URL / browser.cdp_url overrides"
    spawn = (os.environ.get("BU_AUTOSPAWN") or "").strip().lower()
    if spawn in {"1", "true", "yes"}:
        return "scope_denied: managed mode refuses BU_AUTOSPAWN cloud fallback"
    return None


def _apply_lease(env: dict[str, str], lease: dict[str, Any], control: str) -> Optional[str]:
    slot_id = str(lease.get("slot_id") or "")
    generation = str(lease.get("browser_generation") or "")
    epoch = int(lease.get("ownership_epoch") or 0)
    token = str(lease.get("command_token") or "")
    if not (slot_id and generation and token):
        return "unavailable: control acquire omitted slot/generation/token"
    env["BU_CDP_URL"] = f"{control.rstrip('/')}/slot/{slot_id}/json/version?tok={token}"
    env[MANAGED_LEASE_ENV] = str(lease.get("lease_id") or "")
    env[MANAGED_TOKEN_ENV] = token
    env[MANAGED_SLOT_ENV] = slot_id
    env[MANAGED_GENERATION_ENV] = generation
    # Only a counter control actually reported; anything else stays unset so
    # the capture guard fails closed (never auto-clears) for this lease.
    wipe_count = lease.get("wipe_count")
    if isinstance(wipe_count, int) and not isinstance(wipe_count, bool) and wipe_count >= 0:
        env[MANAGED_WIPE_ENV] = str(wipe_count)
    else:
        env.pop(MANAGED_WIPE_ENV, None)
    kind = str(lease.get("profile_kind") or "")
    if kind in {"temporary", "persistent"}:
        env[MANAGED_PROFILE_KIND_ENV] = kind
    else:
        env.pop(MANAGED_PROFILE_KIND_ENV, None)
    env["BU_NAME"] = daemon_cache_key(slot_id, generation, epoch)
    env["_HERMES_BU_PRIVATE_BROWSER"] = "1"
    return None


def _retry_after(reply: dict[str, Any]) -> Optional[float]:
    raw = reply.get("retry_after_s")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(RETRY_AFTER_MIN_S, min(float(raw), RETRY_AFTER_MAX_S))


def busy_fields(reply: dict[str, Any]) -> dict[str, Any]:
    """The queue facts a ``busy`` reply carried, for the model-facing result."""
    fields: dict[str, Any] = {}
    ticket = str(reply.get("ticket") or reply.get("queued") or "").strip()
    if ticket:
        fields["queued_id"] = ticket
    position = reply.get("position")
    if isinstance(position, int) and not isinstance(position, bool) and position > 0:
        fields["position"] = position
    retry = _retry_after(reply)
    if retry is not None:
        fields["retry_after_s"] = retry
    if reply.get("left_queue"):
        fields["left_queue"] = True
    return fields


def _format_error(lease: dict[str, Any]) -> str:
    code = str(lease.get("error") or "unavailable")
    detail = str(lease.get("detail") or "").strip()
    if code == "busy":
        extra = detail or "browser slot queued"
        fields = busy_fields(lease)
        queue = []
        if "queued_id" in fields:
            queue.append(f"ticket {fields['queued_id']}")
        if "position" in fields:
            queue.append(f"position {fields['position']}")
        if "retry_after_s" in fields:
            queue.append(f"retry after {fields['retry_after_s']:g}s")
        if fields.get("left_queue"):
            queue.append("waited, then left the line; call again later")
        where = f" ({', '.join(queue)})" if queue else ""
        return RouteError(
            f"busy: {extra}{where} — retry this call later (or cancel a ticket you still hold); "
            "do not use a local, cloud, or CDP fallback",
            fields,
        )
    if detail:
        return f"{code}: {detail}"
    return code


def busy_wait_budget(timeout_s: Any = None) -> float:
    """Seconds a queued acquire may wait: the env default, at most half the call's timeout."""
    try:
        budget = float(os.environ.get(BUSY_WAIT_ENV) or DEFAULT_BUSY_WAIT_S)
    except ValueError:
        budget = DEFAULT_BUSY_WAIT_S
    if timeout_s is not None:
        try:
            budget = min(budget, float(timeout_s) / 2.0)
        except (TypeError, ValueError):
            pass
    return max(budget, 0.0)


def _post(control: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
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


def get_json(path: str, *, timeout: float = STATUS_TIMEOUT_S) -> dict[str, Any]:
    """Authenticated ``GET`` on control; errors come back as ``{"error", "detail"}``."""
    control = control_url()
    if not control or control == CONFIG_UNREADABLE_URL or is_disabled_url(control):
        return {"error": "unavailable", "detail": "managed control URL missing or disabled"}
    key = control_key()
    if not key:
        return {"error": "unavailable", "detail": f"{MANAGED_KEY_ENV} missing"}
    req = Request(control.rstrip("/") + path, method="GET", headers={"Authorization": f"Bearer {key}"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            return {"error": "unavailable", "detail": f"control HTTP {exc.code}"}
    except (OSError, ValueError) as exc:
        return {"error": "unavailable", "detail": f"control unreachable: {type(exc).__name__}"}
    return data if isinstance(data, dict) else {"error": "unavailable", "detail": "control returned a non-object"}


def post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Authenticated ``POST`` on control (same error shape as :func:`get_json`)."""
    control = control_url()
    if not control or control == CONFIG_UNREADABLE_URL or is_disabled_url(control):
        return {"error": "unavailable", "detail": "managed control URL missing or disabled"}
    if not control_key():
        return {"error": "unavailable", "detail": f"{MANAGED_KEY_ENV} missing"}
    return _post(control, path, payload)


def slot_status(slot_id: str) -> dict[str, Any]:
    """``GET /v1/status?slot=<id>``: one slot's public row, or an error dict."""
    return get_json("/v1/status?" + urlencode({"slot": slot_id}))


# Rollout escape hatch: a control without the capture-guard route (404) lets
# a card fill go ahead on the local mark alone. Off by default (fail closed).
CAPTURE_GUARD_OPTIONAL_ENV = "HERMES_BROWSER_CAPTURE_GUARD_OPTIONAL"


def set_capture_guard(lease_id: str, command_token: str) -> tuple[bool, str]:
    """``POST /v1/capture-guard``: control refuses capture on the lease's slot from now on.

    Returns ``(ok, reason)``. ``reason`` is ``""`` on success,
    ``control_without_capture_guard`` on a 404 (control older than the
    route), else a short cause. The flag is set-only: only a temporary-profile
    wipe or a human clears it (contract: monolith ``services/browser/README.md``).
    """
    control = control_url()
    if not control or control == CONFIG_UNREADABLE_URL or is_disabled_url(control):
        return False, "managed control URL missing or disabled"
    key = control_key()
    if not key:
        return False, f"{MANAGED_KEY_ENV} missing"
    if not lease_id or not command_token:
        return False, "no managed lease for this page"
    req = Request(
        control.rstrip("/") + "/v1/capture-guard",
        data=json.dumps({"lease_id": lease_id, "command_token": command_token}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urlopen(req, timeout=STATUS_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        if exc.code == 404:
            return False, "control_without_capture_guard"
        return False, f"control HTTP {exc.code}"
    except (OSError, ValueError) as exc:
        return False, f"control unreachable: {type(exc).__name__}"
    if not isinstance(data, dict) or data.get("capture_blocked") is not True:
        return False, "control did not confirm the guard"
    return True, ""


def capture_guard_optional() -> bool:
    return (os.environ.get(CAPTURE_GUARD_OPTIONAL_ENV) or "").strip().lower() in {"1", "true", "yes"}


def held_leases_for_task(task_id: Optional[str]) -> list[dict[str, str]]:
    """Copies of the lease snapshots this process holds for ``task_id``, each
    with its ``identity`` and ``mode`` from the hold key. Tokens included:
    callers are trusted tool code and must never return them to the model."""
    owner = str(task_id or "").strip() or "default"
    out: list[dict[str, str]] = []
    with _held_lock:
        for key, snapshot in _held_leases.items():
            task, _, rest = key.partition("|")
            identity, _, mode = rest.partition("|")
            if task == owner:
                out.append({**snapshot, "identity": identity, "mode": mode})
    return out


# Handoffs this process minted: (task, slot, agent lease) -> expiry (monotonic). The gateway's
# handback route wakes a session only for a handoff it minted or a lease it still holds.
_HANDOFF_MAX_S = 2 * 3600.0
_handoffs_lock = threading.Lock()
_minted_handoffs: dict[tuple[str, str, str], float] = {}


def note_handoff(task_id: Optional[str], slot_id: str, lease_id: str, expires_in: Any = None) -> None:
    try:
        ttl = min(max(float(expires_in or 0), 0.0) + 1800.0, _HANDOFF_MAX_S)
    except (TypeError, ValueError):
        ttl = _HANDOFF_MAX_S
    key = (str(task_id or "").strip() or "default", str(slot_id or ""), str(lease_id or ""))
    now = _monotonic()
    with _handoffs_lock:
        for stale in [k for k, until in _minted_handoffs.items() if until <= now]:
            _minted_handoffs.pop(stale, None)
        _minted_handoffs[key] = now + ttl


def claim_handback(task_id: str, slot_id: str, lease_id: str) -> bool:
    """True (once) when ``task_id`` minted a handoff for this slot + agent lease, or still
    holds that lease. A minted handoff is consumed by the claim."""
    key = (str(task_id or "").strip() or "default", str(slot_id or ""), str(lease_id or ""))
    if not key[1] or not key[2]:
        return False
    now = _monotonic()
    with _handoffs_lock:
        until = _minted_handoffs.pop(key, 0.0)
    if until > now:
        return True
    return any(lease.get(MANAGED_SLOT_ENV) == key[1] and lease.get(MANAGED_LEASE_ENV) == key[2]
               for lease in held_leases_for_task(key[0]))


def _forget_held(key: str) -> None:
    with _held_lock:
        _held_leases.pop(key, None)


@contextmanager
def _single_flight(key: str) -> Iterator[None]:
    """Serialize resolve calls for one hold key; other keys stay concurrent.

    Two parallel ``browser_exec`` calls in one turn (same task, identity and
    mode) both missed the cache and both acquired; on a single-slot identity
    the second came back ``busy``, queued behind its own task's lease. The
    second caller now waits here and reuses the lease the first one stored.
    Lock order is always key lock -> ``_held_lock``: ``_held_lock`` is only
    taken briefly to look up / refcount the key lock, never held across the
    network call.
    """
    with _held_lock:
        entry = _key_locks.get(key)
        if entry is None:
            entry = _key_locks[key] = [threading.Lock(), 0]
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _held_lock:
            entry[1] -= 1
            if entry[1] == 0 and _key_locks.get(key) is entry:
                del _key_locks[key]


def _acquire(
    control: str,
    *,
    task: str,
    mode: str,
    identity: str,
    qa_targets: list[str],
    ticket: str = "",
) -> dict[str, Any]:
    if control == CONFIG_UNREADABLE_URL:
        return {
            "error": "unavailable",
            "detail": "config.yaml unreadable; refusing unmanaged browser fallback",
        }
    if is_disabled_url(control):
        return {"error": "unavailable", "detail": "browsing disabled by operator"}
    if not control_key():
        return {"error": "unavailable", "detail": f"{MANAGED_KEY_ENV} missing"}
    payload: dict[str, Any] = {
        "task": task or "browser-exec",
        "mode": mode,
        "identity": identity,
        "qa_targets": qa_targets,
    }
    if ticket:
        payload["ticket"] = ticket
    return _post(control, "/v1/acquire", payload)


def _acquire_waiting(
    control: str,
    *,
    task: str,
    mode: str,
    identity: str,
    qa_targets: list[str],
    wait_s: float,
) -> dict[str, Any]:
    """Acquire, waiting in control's queue for at most ``wait_s`` seconds.

    Control keeps the waiters' order (one entry per task/identity/mode,
    refreshed by each retry) and answers ``busy`` with a ticket and
    ``retry_after_s``. Only a queued reply is waited on: a slot held by a
    human for this task, or a refusal without a ticket, returns at once.
    A ``stale`` reply (reconnect with an old generation/epoch) is retried once.
    """
    started = _monotonic()
    deadline = started + max(wait_s, 0.0)
    ticket = ""
    stale_retried = False
    try:
        while True:
            lease = _acquire(
                control, task=task, mode=mode, identity=identity, qa_targets=qa_targets,
                **({"ticket": ticket} if ticket else {}),
            )
            if lease.get("error") == "stale" and not stale_retried:
                stale_retried = True
                continue
            if lease.get("error") != "busy":
                return lease
            ticket = str(lease.get("ticket") or lease.get("queued") or "")
            if not ticket:
                return lease
            _remember_ticket(task, ticket)
            pause = _retry_after(lease) or DEFAULT_RETRY_AFTER_S
            if wait_s <= 0:
                return lease  # no budget: report the ticket, the caller decides
            if _monotonic() + pause > deadline or not _pause(pause):
                # Gave up or /stop: leave the line so an idle slot is not held for a waiter that left.
                _cancel_ticket(control, task, ticket)
                lease = {k: v for k, v in lease.items() if k not in {"queued", "ticket"}}
                lease["left_queue"] = True
                return lease
    finally:
        _wait_state.waited = _monotonic() - started


def _pause(seconds: float) -> bool:
    """Sleep in short slices; False as soon as the turn is interrupted (/stop)."""
    end = _monotonic() + seconds
    while True:
        if _interrupted():
            return False
        left = end - _monotonic()
        if left <= 0:
            return True
        _sleep(min(left, 1.0))


def _interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted
    except ImportError:
        return False
    return is_interrupted()


_tickets_lock = threading.Lock()
_issued_tickets: dict[str, set[str]] = {}
_wait_state = threading.local()


def _remember_ticket(task: str, ticket: str) -> None:
    with _tickets_lock:
        tickets = _issued_tickets.setdefault(task, set())
        tickets.add(ticket)
        if len(tickets) > 64:
            tickets.pop()


def ticket_issued_to(task_id: Optional[str], ticket: str) -> bool:
    with _tickets_lock:
        return ticket in _issued_tickets.get(str(task_id or "browser-exec"), set())


def _cancel_ticket(control: str, task: str, ticket: str) -> None:
    """Best effort ``POST /v1/cancel``; control refuses another task's ticket."""
    _post(control, "/v1/cancel", {"request_id": ticket, "task": task})
    with _tickets_lock:
        _issued_tickets.get(task, set()).discard(ticket)


def last_busy_wait_s() -> float:
    """Seconds this thread's last managed acquire spent waiting in line."""
    return float(getattr(_wait_state, "waited", 0.0) or 0.0)


def resolve_managed_cdp(
    env: dict[str, str],
    task_id: Optional[str] = "",
    session_name: str = "",
    mode: str = "",
    identity: str = "",
    qa_targets: Any = None,
    raw_cdp_override: Optional[str] = None,
    reconnect: bool = False,
    wait_s: Optional[float] = None,
) -> Optional[str]:
    """Return an error string, or None after pointing ``env`` at the gate.

    Callers must invoke this only when :func:`is_managed` is true so a
    successful None does not fall through to local/cloud backends.

    A ``stale`` acquire (reconnect with an old generation/epoch) is retried
    once. ``busy`` waits in control's fair queue for up to ``wait_s``
    (default 0: no wait), then leaves the queue and returns a
    :class:`RouteError` carrying the position; never fall back.
    """
    _wait_state.waited = 0.0
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
    with _single_flight(key):
        return _resolve_held(
            env,
            control,
            key,
            task=str(task_id or session_name or "browser-exec"),
            mode=payload_mode,
            identity=payload_identity,
            qa_targets=_coerce_targets(qa_targets),
            reconnect=reconnect,
            # Only browser_exec passes a budget; built-in paths and gates never wait.
            wait_s=0.0 if wait_s is None else max(float(wait_s), 0.0),
        )


def _resolve_held(
    env: dict[str, str],
    control: str,
    key: str,
    *,
    task: str,
    mode: str,
    identity: str,
    qa_targets: list[str],
    reconnect: bool,
    wait_s: float = 0.0,
) -> Optional[str]:
    """Reuse the live held lease for ``key`` or acquire one. Caller holds the key lock."""
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
        _retire_daemon(cached.get("BU_NAME", ""))

    lease = _acquire_waiting(
        control,
        task=task,
        mode=mode,
        identity=identity,
        qa_targets=qa_targets,
        wait_s=wait_s,
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
        MANAGED_SLOT_ENV: env.get(MANAGED_SLOT_ENV, ""),
        MANAGED_GENERATION_ENV: env.get(MANAGED_GENERATION_ENV, ""),
        **{k: env[k] for k in (MANAGED_WIPE_ENV, MANAGED_PROFILE_KIND_ENV) if k in env},
        "BU_NAME": env.get("BU_NAME", ""),
        "_HERMES_BU_PRIVATE_BROWSER": "1",
    }
    with _held_lock:
        _held_leases[key] = snapshot
    return None


def managed_cdp_or_error(
    env: Optional[dict[str, str]] = None,
    *,
    task_id: str = "",
    session_name: str = "",
    mode: str = "",
    identity: str = "",
    qa_targets: Any = None,
    raw_cdp_override: Optional[str] = None,
    reconnect: bool = False,
    wait_s: Optional[float] = None,
) -> tuple[Optional[str], Optional[str]]:
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
        wait_s=wait_s,
    )
    if err:
        return None, err
    return hold.get("BU_CDP_URL") or None, None


def release_lease(env: dict[str, str]) -> Optional[str]:
    """Best-effort ``POST /v1/release`` with lease id + command token."""
    control = control_url()
    lease_id = str(env.get(MANAGED_LEASE_ENV) or "").strip()
    token = str(env.get(MANAGED_TOKEN_ENV) or "").strip()
    if not control or not lease_id:
        return None
    if is_disabled_url(control):
        return OPERATOR_DISABLED_ERROR
    cache_name = str(env.get("BU_NAME") or "").strip()
    retired = {cache_name}
    with _held_lock:
        for key, held in list(_held_leases.items()):
            if held.get(MANAGED_LEASE_ENV) == lease_id or (
                cache_name and held.get("BU_NAME") == cache_name
            ):
                retired.add(str(held.get("BU_NAME") or ""))
                _held_leases.pop(key, None)
    for name in retired:
        _retire_daemon(name)
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


def _harden_key_holder() -> None:
    """The process that holds the control key (the gateway) keeps it out of same-uid
    ``/proc/<pid>/environ`` reads (``tools/browser_process_hardening.py``)."""
    if (os.environ.get(MANAGED_KEY_ENV) or os.environ.get(MANAGED_KEY_FILE_ENV) or "").strip():
        from tools.browser_process_hardening import make_undumpable

        make_undumpable()


_harden_key_holder()
