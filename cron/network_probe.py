"""Is this host offline right now? One shared answer per minute.

The message classifier (``cron.transient_network``) knows the network phrases, but the failures
an outage really produces are often worded by the job itself ("push refused (HTTP no answer)",
"refresh failed (exit 1)", "Script timed out after 180s"). When a run fails the scheduler asks
here: resolve and TCP-connect to a few well-known HTTPS endpoints, all at once, under one overall
deadline (DNS included). Host offline means every probe target answered "unreachable" with a
connectivity error before the deadline; one reachable target means the host is online, so a single
remote service being down never silences anything. The answer is cached for ``CACHE_SECONDS`` in
the process and in a small JSON file under ``HERMES_HOME`` (managed jobs run in separate worker
processes, so 14 jobs failing in one tick still cost one probe, not 14).

Fails open: a deadline, a local resource error (EMFILE, EACCES, ...) or any other unexpected error
says "online", i.e. the normal alert path. No lock is held across network I/O.
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import queue
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cron.network_probe")

PROBE_TARGETS: tuple[tuple[str, int], ...] = (("api.telegram.org", 443), ("github.com", 443))
CONNECT_TIMEOUT_SECONDS = 3.0
PROBE_DEADLINE_SECONDS = 5.0
CACHE_SECONDS = 60.0
_CLOCK_SKEW_SECONDS = 5.0  # a stamp this far ahead of our clock is still accepted
_MAX_CACHE_BYTES = 4096
_CACHE_FILE = ("cron", ".network_probe.json")

# Only these mean "the network is not there"; any other failure is a local problem, not an outage.
_CONNECTIVITY_ERRNOS = frozenset(
    getattr(errno, name) for name in ("EHOSTUNREACH", "ENETUNREACH", "ENETDOWN") if hasattr(errno, name))
_CONNECTIVITY_ERRORS = (
    ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, socket.timeout, TimeoutError,
    socket.gaierror,
)

_lock = threading.Lock()  # guards ``_cached`` only, never held across network or file I/O
_cached: Optional[tuple[float, bool]] = None  # (monotonic stamp, offline)


def _is_connectivity_error(exc: BaseException) -> bool:
    return isinstance(exc, _CONNECTIVITY_ERRORS) or (
        isinstance(exc, OSError) and exc.errno in _CONNECTIVITY_ERRNOS)


def _reachable(host: str, port: int) -> bool:
    """Resolve *host* and open a TCP connection to it. False only for a connectivity error; any
    other failure (descriptor exhaustion, permissions, ...) is not evidence of an outage, so True."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS):
            return True
    except Exception as exc:  # health: allow BLE001 -- only connectivity errors count as unreachable
        return not _is_connectivity_error(exc)


def _probe_offline() -> bool:
    """Probe every target concurrently (daemon threads: DNS ignores socket timeouts) and wait at
    most ``PROBE_DEADLINE_SECONDS`` overall. Offline only when every target reported unreachable in
    time; a reachable target, a missed deadline or a failing probe thread means online."""
    results: queue.Queue[bool] = queue.Queue()

    def _run(host: str, port: int) -> None:
        try:
            results.put(bool(_reachable(host, port)))
        except Exception:  # health: allow BLE001 -- a broken probe must not silence alerts
            results.put(True)

    from agent.memory_provider import spawn_context_thread

    for host, port in PROBE_TARGETS:
        spawn_context_thread(_run, name="network-probe", args=(host, port)).start()
    deadline = time.monotonic() + PROBE_DEADLINE_SECONDS
    for _ in PROBE_TARGETS:
        try:
            if results.get(timeout=max(deadline - time.monotonic(), 0.0)):
                return False
        except queue.Empty:
            return False
    return True


def _cache_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home().joinpath(*_CACHE_FILE)


def _read_shared() -> Optional[tuple[bool, float]]:
    """``(offline, age_seconds)`` of the answer another process stored less than ``CACHE_SECONDS``
    ago, else None. Never blocks (non-blocking open, regular files only, tiny read cap) and accepts
    only a JSON bool ``online`` plus a finite ``checked_at`` that is not in the future."""
    fd = None
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(_cache_path(), flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _MAX_CACHE_BYTES + 1)
        if len(raw) > _MAX_CACHE_BYTES:
            return None
        data = json.loads(raw.decode("utf-8-sig"))
        online, checked_at = data["online"], data["checked_at"]
        if type(online) is not bool or type(checked_at) not in (int, float) or not math.isfinite(checked_at):
            return None
        age = time.time() - checked_at
        if -_CLOCK_SKEW_SECONDS <= age < CACHE_SECONDS:
            return (not online, max(age, 0.0))
    except Exception:  # health: allow BLE001 -- missing, torn or foreign file: just probe
        logger.debug("network probe: no usable shared result", exc_info=True)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    return None


def _write_shared(offline: bool) -> None:
    """Best-effort atomic write; an unwritable HERMES_HOME leaves only the in-process cache."""
    tmp_name = None
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".network_probe.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"checked_at": time.time(), "online": not offline}, handle)
        os.replace(tmp_name, path)
        tmp_name = None
    except Exception:  # health: allow BLE001 -- the shared cache is an optimisation only
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def box_is_offline() -> bool:
    """True when every probe target is unreachable. Cached for ``CACHE_SECONDS`` (process + file)."""
    global _cached
    now = time.monotonic()
    with _lock:
        if _cached is not None and now - _cached[0] < CACHE_SECONDS:
            return _cached[1]
    shared = _read_shared()
    if shared is not None:
        offline, age = shared
        with _lock:
            _cached = (now - age, offline)  # expires at the original checked_at + CACHE_SECONDS
        return offline
    try:
        offline = _probe_offline()
    except Exception:  # health: allow BLE001 -- a broken probe must not silence alerts
        offline = False
    with _lock:
        _cached = (time.monotonic(), offline)
    _write_shared(offline)
    return offline


def reset_cache() -> None:
    """Forget the cached answer, in process and on disk (tests)."""
    global _cached
    with _lock:
        _cached = None
    try:
        _cache_path().unlink()
    except Exception:  # health: allow BLE001 -- nothing to forget
        logger.debug("network probe: no shared result to remove", exc_info=True)
