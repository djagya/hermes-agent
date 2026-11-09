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
# dlz: fault-hunt B10. Control tells the session when a handoff ended WITHOUT Return to Sera (the
# human ended the slot, the link expired unused, the slot parked or was recycled). Only a reason code
# from this fixed table reaches the text; anything else reads as "other".
HANDOFF_ENDED_REASONS = {
    "human_ended": "Danil pressed End on the slot",
    "link_expired": "the handoff link expired unused",
    "human_idle": "the human session went idle and the slot parked",
    "human_disconnected": "the human viewer disconnected and the slot parked",
    "take_failed": "the takeover failed and the slot parked",
    "resume_failed": "Return to Sera failed and the slot parked",
    "recycled": "control recycled the slot",
    "other": "control ended it",
}
HANDOFF_ENDED_TEXT = (
    "[browser-control] The handoff of browser slot {slot} ended without Return to Sera ({why}). "
    "The page Danil was on may be gone, and nothing he did there is confirmed. Do not assume the step "
    "was finished: your next browser_exec gets a fresh lease; check the page's real state there, and "
    "tell Danil what you found and what still needs him."
)
EVENTS = ("handback", "handoff_ended")
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
    """``({"event", "session_id", "slot_id", "lease_id", "resumed_lease_id", "reason_code"}, "")`` or
    ``(None, reason)``. ``resumed_lease_id`` is "" unless well formed; ``reason_code`` is a key of
    :data:`HANDOFF_ENDED_REASONS` ("" for a handback)."""
    event = payload.get("event") if isinstance(payload, dict) else None
    if event not in EVENTS:
        return None, "event must be 'handback' or 'handoff_ended'"
    session_id = str(payload.get("session_id") or "").strip()
    slot_id = str(payload.get("slot_id") or "").strip()
    lease_id = str(payload.get("lease_id") or "").strip()
    if not _SESSION_RE.match(session_id):
        return None, "session_id missing or malformed"
    if not _SLOT_RE.match(slot_id):
        return None, "slot_id missing or malformed"
    if not _SLOT_RE.match(lease_id):
        return None, "lease_id missing or malformed"
    resumed = str(payload.get("resumed_lease_id") or "").strip()
    reason = ""
    if event == "handoff_ended":
        reason = str(payload.get("reason_code") or "").strip()
        reason = reason if reason in HANDOFF_ENDED_REASONS else "other"
    return {"event": str(event), "session_id": session_id, "slot_id": slot_id, "lease_id": lease_id,
            "resumed_lease_id": resumed if _SLOT_RE.match(resumed) else "", "reason_code": reason}, ""


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


async def inject(runner: Any, session_id: str, slot_id: str, lease_id: str, *, event: str = "handback",
                 reason_code: str = "") -> str:
    """Schedule the wake turn. Returns ``"injected"``, ``"replay"``, ``"session_not_found"`` or ``"unavailable"``.

    The control key is readable by same-uid model code, so the key alone never wakes a session: the
    session must have minted a handoff for this slot + agent lease (or still hold that lease), and
    each lease wakes at most once per replay window."""
    if runner is None:
        return "unavailable"
    from tools.browser_control_route import claim_handback

    if not claim_handback(session_id, slot_id, lease_id):
        # dlz: D10-B, fail closed. After a gateway restart the in-memory claim is gone, and control-v1's
        # /v1/status?lease= does not attest which agent lease a Return resumed, so a live lease for this
        # task/slot proves nothing about Return. Refuse (the caller logs it loudly); never wake on it.
        return "session_not_found"
    # Once per original lease and event, only after the claim (a forged id cannot burn the real one).
    # dlz: recheck P2 — a handoff_ended (an unused link expired) must not eat the Return of a newer link
    # on the same lease, so each event has its own key.
    if _seen(lease_id if event == "handback" else f"{event}:{lease_id}"):
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
    if event == "handoff_ended":
        why = HANDOFF_ENDED_REASONS.get(reason_code) or HANDOFF_ENDED_REASONS["other"]
        content = HANDOFF_ENDED_TEXT.format(slot=slot_id, why=why)
    else:
        content = HANDBACK_TEXT.format(slot=slot_id)
    ok = schedule(content=content, plugin_id=PLUGIN_ID, session_key=entry.session_key)
    if not ok:
        return "unavailable"
    if event == "handback":
        from tools.browser_exec_dialogs import note_handoff

        note_handoff(session_id)  # the next exec holds beforeunload: the page may hold Danil's unsaved input
    logger.info("browser %s wake scheduled: slot=%s session=%s reason=%s", event, slot_id, session_id,
                reason_code or "-")
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
    outcome = await inject(runner, fields["session_id"], fields["slot_id"], fields["lease_id"],
                           event=fields["event"], reason_code=fields["reason_code"])
    if outcome == "replay":
        return _error("This handback was already delivered.", 409, "handback_replay")
    if outcome == "session_not_found":
        # dlz: D10-B — loud, not silent: a 404 here means Sera is never woken and the slot recycles later.
        logger.warning("browser %s NOT delivered: no local handoff claim (lost on a gateway restart?) "
                       "slot=%s session=%s",
                       fields["event"], fields["slot_id"], fields["session_id"])
        return _error("No live session with that id.", 404, "session_not_found")
    if outcome != "injected":
        return _error("Gateway cannot route the wake turn now.", 503, "handback_unavailable")
    return web.json_response({"injected": True}, status=202)


def _http_routes(adapter: Any) -> list[tuple]:
    return [("POST", HANDBACK_PATH, partial(handle_request, adapter))]
