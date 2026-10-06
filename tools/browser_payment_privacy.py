"""Profile/task-scoped screenshot guard after native card ingress.

This prevents accidental capture through standard browser tools; arbitrary
terminal/code execution is not a containment boundary (see SECURITY.md).
"""

from __future__ import annotations

import threading

from hermes_constants import get_hermes_home

_LOCK = threading.Lock()
_SENSITIVE: set[tuple[str, str]] = set()


def mark_payment_session(task_id: str) -> None:
    with _LOCK:
        _SENSITIVE.add((str(get_hermes_home()), task_id))


def payment_session_sensitive(task_id: str | None) -> bool:
    with _LOCK:
        home = str(get_hermes_home())
        # Sidecar aliases and desktop captures can view the same browser. Once
        # card ingress occurs, capture is refused for the entire current profile.
        return any(profile == home for profile, _task in _SENSITIVE)


def screenshot_refusal() -> dict:
    return {
        "success": False,
        "error_type": "payment_screenshot_blocked",
        "error": "Screenshots/PDF/recordings are disabled for this task after card fill. Use redacted text inspection.",
    }
