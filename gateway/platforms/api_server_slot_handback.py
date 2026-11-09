"""Wake a session when a human returns its managed browser slot.

Monolith browser-control hands a slot to a human (handoff link or the slot
board) and back again with **Return to Sera**. Until now the agent only learned
of the return when the human also typed in chat. Control now calls
``POST /v1/browser-slots/handback`` on this API server with the same bearer key
the agent uses for control (``HERMES_BROWSER_CONTROL_KEY`` or
``HERMES_BROWSER_CONTROL_KEY_FILE``), naming the session id that holds the
lease and the agent lease that was handed off. The gateway wakes the session
only when that session minted a handoff for exactly that slot and lease (or
still holds the lease) — the control key is readable by same-uid code, so the
key alone must not be enough — and each lease wakes it at most once per
10 minutes (409 otherwise). It injects one internal turn whose text it
composes itself: nothing from the request body reaches the model except a
validated slot id.

Not the API key: the API key dispatches arbitrary agent work, and control only
ever needs this one notice. A key-less gateway answers 503.
"""

from __future__ import annotations

import hmac
import logging
import re
import threading
import time
from functools import partial
from typing import Any, Optional

logger = logging.getLogger(__name__)

HANDBACK_PATH = "/v1/browser-slots/handback"
PLUGIN_ID = "browser-control-handback"
_SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@-]{0,200}$")

HANDBACK_TEXT = (
    "[browser-control] Danil pressed Return to Sera: browser slot {slot} is back with this task. "
    "This only means the browser is back; it is not approval for any purchase, deletion or other step "
    "that still needs Danil's confirmation. Your next browser_exec reclaims the same browser; switch_tab() "
    "to the saved handoff target (workspace file handoff-target) before reading or clicking anything, then "
    "check the page's fresh state. If that tab is gone, stop and tell Danil."
)
REPLAY_WINDOW_S = 600.0
_replay_lock = threading.Lock()
_handled: dict[str, float] = {}


def _expected_key() -> str:
    try:
        from tools.browser_control_route import control_key

        return control_key()
    except ImportError:
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
    lease_id = str(payload.get("lease_id") or "").strip()
    if not _SESSION_RE.match(session_id):
        return None, "session_id missing or malformed"
    if not _SLOT_RE.match(slot_id):
        return None, "slot_id missing or malformed"
    if not _SLOT_RE.match(lease_id):
        return None, "lease_id missing or malformed"
    return {"session_id": session_id, "slot_id": slot_id, "lease_id": lease_id}, ""


def _seen(lease_id: str) -> bool:
    """True when this lease's handback was already handled within the replay window; records it otherwise."""
    now = time.monotonic()
    with _replay_lock:
        for old in [k for k, at in _handled.items() if now - at > REPLAY_WINDOW_S]:
            _handled.pop(old, None)
        if lease_id in _handled:
            return True
        _handled[lease_id] = now
        return False


async def inject(runner: Any, session_id: str, slot_id: str, lease_id: str) -> str:
    """Schedule the wake turn. Returns ``"injected"``, ``"replay"``, ``"session_not_found"`` or ``"unavailable"``.

    The control key is readable by same-uid model code, so the key alone never wakes a session: the
    session must have minted a handoff for this slot + agent lease (or still hold that lease), and
    each lease wakes at most once per replay window."""
    if runner is None:
        return "unavailable"
    from tools.browser_control_route import claim_handback

    if not claim_handback(session_id, slot_id, lease_id):
        return "session_not_found"
    if _seen(lease_id):  # only after the claim: a forged id cannot burn the real handback
        return "replay"
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
    from tools.browser_exec_dialogs import note_handoff

    note_handoff(session_id)  # the next exec holds beforeunload: the page may hold Danil's unsaved input
    logger.info("browser handback wake scheduled: slot=%s session=%s", slot_id, session_id)
    return "injected"


def _error(message: str, status: int, code: str):
    from gateway.platforms.api_server import _error_response

    return _error_response(message, status, code=code)


async def handle_request(adapter: Any, request: Any):
    """``POST /v1/browser-slots/handback``: 503 no key, 401 bad Bearer, 400 bad body, 404 unknown session."""
    from aiohttp import web

    status = check_bearer(request.headers.get("Authorization", ""))
    if status == 503:
        return _error("Browser slot handback is not configured.", 503, "handback_disabled")
    if status is not None:
        return _error("Invalid handback authorization.", 401, "handback_unauthorized")
    try:
        payload = await request.json()
    except ValueError:
        return _error("Request body must be valid JSON.", 400, "invalid_json")
    fields, reason = parse(payload)
    if fields is None:
        return _error(reason, 400, "handback_invalid")
    runner = adapter.gateway_runner or request.app.get("gateway_runner")
    outcome = await inject(runner, fields["session_id"], fields["slot_id"], fields["lease_id"])
    if outcome == "replay":
        return _error("This handback was already delivered.", 409, "handback_replay")
    if outcome == "session_not_found":
        return _error("No live session with that id.", 404, "session_not_found")
    if outcome != "injected":
        return _error("Gateway cannot route the wake turn now.", 503, "handback_unavailable")
    return web.json_response({"injected": True}, status=202)


def _http_routes(adapter: Any) -> list[tuple]:
    return [("POST", HANDBACK_PATH, partial(handle_request, adapter))]
