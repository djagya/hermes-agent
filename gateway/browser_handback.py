"""Wake a session when a human returns its managed browser slot.

Monolith browser-control hands a slot to a human (handoff link or the slot
board) and back again with **Return to Sera**. Until now the agent only learned
of the return when the human also typed in chat. Control now calls
``POST /v1/browser-slots/handback`` on this API server with the same bearer key
the agent uses for control (``HERMES_BROWSER_CONTROL_KEY`` or
``HERMES_BROWSER_CONTROL_KEY_FILE``), naming the session id that holds the
lease. The gateway looks the session up and injects one internal turn whose
text it composes itself: nothing from the request body reaches the model
except a validated slot id.

Not the API key: the API key dispatches arbitrary agent work, and control only
ever needs this one notice. A key-less gateway answers 503.
"""

from __future__ import annotations

import hmac
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

HANDBACK_PATH = "/v1/browser-slots/handback"
PLUGIN_ID = "browser-control-handback"
_SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@-]{0,200}$")

HANDBACK_TEXT = (
    "[browser-control] Danil pressed Return to Sera: browser slot {slot} is back with this task. "
    "Continue the task now. Your next browser_exec reclaims the same browser; switch_tab() to the saved "
    "handoff target (workspace file handoff-target) before reading or clicking anything, then check the "
    "page's fresh state. If that tab is gone, stop and tell Danil."
)


def _expected_key() -> str:
    try:
        from tools.browser_control_route import control_key

        return control_key()
    except Exception:  # health: allow BLE001 -- no control client: the route is disabled
        return ""


def check_bearer(header: str) -> Optional[int]:
    """None when authorized, else the HTTP status to answer (401 bad key, 503 no key configured)."""
    expected = _expected_key()
    if not expected:
        return 503
    scheme, _, offered = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not offered:
        return 401
    return None if hmac.compare_digest(offered.strip().encode(), expected.encode()) else 401


def parse(payload: Any) -> tuple[Optional[dict[str, str]], str]:
    """``({"session_id", "slot_id"}, "")`` or ``(None, reason)``."""
    if not isinstance(payload, dict) or payload.get("event") != "handback":
        return None, "event must be 'handback'"
    session_id = str(payload.get("session_id") or "").strip()
    slot_id = str(payload.get("slot_id") or "").strip()
    if not _SESSION_RE.match(session_id):
        return None, "session_id missing or malformed"
    if not _SLOT_RE.match(slot_id):
        return None, "slot_id missing or malformed"
    return {"session_id": session_id, "slot_id": slot_id}, ""


async def inject(runner: Any, session_id: str, slot_id: str) -> str:
    """Schedule the wake turn. Returns ``"injected"``, ``"session_not_found"`` or ``"unavailable"``."""
    if runner is None:
        return "unavailable"
    store = getattr(runner, "async_session_store", None)
    entry = None
    if store is not None and hasattr(store, "lookup_by_session_id"):
        entry = await store.lookup_by_session_id(session_id)
    elif getattr(runner, "session_store", None) is not None:
        entry = runner.session_store.lookup_by_session_id(session_id)
    if entry is None or not getattr(entry, "session_key", ""):
        return "session_not_found"
    schedule = getattr(runner, "_schedule_plugin_message_injection", None)
    if schedule is None:
        return "unavailable"
    ok = schedule(content=HANDBACK_TEXT.format(slot=slot_id), plugin_id=PLUGIN_ID, session_key=entry.session_key)
    if not ok:
        return "unavailable"
    logger.info("browser handback wake scheduled: slot=%s session=%s", slot_id, session_id)
    return "injected"
