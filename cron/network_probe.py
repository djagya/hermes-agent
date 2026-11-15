"""Is this host offline right now? One cached answer per scheduler tick.

The message classifier (``cron.transient_network``) knows the network phrases, but the failures
an outage really produces are often worded by the job itself ("push refused (HTTP no answer)",
"refresh failed (exit 1)", "Script timed out after 180s"). When a run fails the scheduler asks
here once: resolve and TCP-connect to a few well-known HTTPS endpoints with a short timeout. Host
offline means every probe target is unreachable; one reachable target means the host is online,
so a single remote service being down never silences anything. The answer is cached for
``CACHE_SECONDS`` so 14 jobs failing in one tick cost one probe, not 14.

Fails open: any unexpected error says "online", i.e. the normal alert path.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional

PROBE_TARGETS: tuple[tuple[str, int], ...] = (("api.telegram.org", 443), ("github.com", 443))
CONNECT_TIMEOUT_SECONDS = 3.0
CACHE_SECONDS = 60.0

_lock = threading.Lock()
_cached: Optional[tuple[float, bool]] = None  # (monotonic stamp, offline)


def _reachable(host: str, port: int) -> bool:
    """Resolve *host* and open a TCP connection to it; any failure is "not reachable"."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _probe_offline() -> bool:
    return not any(_reachable(host, port) for host, port in PROBE_TARGETS)


def box_is_offline() -> bool:
    """True when no probe target is reachable. Cached for ``CACHE_SECONDS``."""
    global _cached
    with _lock:
        now = time.monotonic()
        if _cached is not None and now - _cached[0] < CACHE_SECONDS:
            return _cached[1]
        try:
            offline = _probe_offline()
        except Exception:  # health: allow BLE001 -- a broken probe must not silence alerts
            offline = False
        _cached = (now, offline)
        return offline


def reset_cache() -> None:
    """Forget the cached answer (tests)."""
    global _cached
    with _lock:
        _cached = None
