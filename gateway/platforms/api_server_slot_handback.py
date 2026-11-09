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

dlz: durable delivery (Codex qol #10). Control keeps each event in an outbox and retries
transient failures with the same ``event_id``. The replay marker is recorded and the claim
consumed only after the wake turn is scheduled, so a 503 ("not admitted, retry") never burns
the real wake. After a gateway restart the in-memory claim is gone; the route then asks
control (``GET /v1/handback/pending?event_id=``) whether it really sent this exact event and
still holds it undelivered. Persisting claims in the gateway's own state was rejected: that
state is writable by the same uid that can read the key, so a forged claim plus the key
would wake Sera; control's outbox lives in another container. Answers: 202 admitted, 409
already handled, 503 not admitted now (retry), 403 refused (no claim, control does not
confirm), 404 no such session.

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
    "expired_unused": "Danil opened the handoff link after it expired and asked for a new one",
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
# dlz: Danil pressed "Ask Sera for a new link" on an expired, never-used link. The slot never
# left this task; he is waiting for a fresh link.
EXPIRED_UNUSED_TEXT = (
    "[browser-control] Danil opened the handoff link for browser slot {slot} after it had expired "
    "(nothing was handed over) and asked for a new one. If the step still needs him, go back to the "
    "saved handoff target (workspace file handoff-target; switch_tab() first, and if that tab is gone "
    "reopen the page), mint a fresh link with browser_control action=handoff and send it to him. If it "
    "is no longer needed, tell him. This is not approval for any purchase, deletion or other step."
)
EVENTS = ("handback", "handoff_ended")
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REPLAY_WINDOW_S = 600.0
# dlz: how long an admitted event id is remembered: longer than control's 30 min retry horizon.
EVENT_RECEIPT_S = 3600.0
_replay_lock = threading.Lock()
_handled: dict[str, float] = {}
_inflight: set[str] = set()


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
    """``({"event", "session_id", "slot_id", "lease_id", "resumed_lease_id", "reason_code",
    "event_id"}, "")`` or ``(None, reason)``. ``resumed_lease_id`` and ``event_id`` are "" unless
    well formed; ``reason_code`` is a key of :data:`HANDOFF_ENDED_REASONS` ("" for a handback)."""
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
    event_id = str(payload.get("event_id") or "").strip()
    return {"event": str(event), "session_id": session_id, "slot_id": slot_id, "lease_id": lease_id,
            "resumed_lease_id": resumed if _SLOT_RE.match(resumed) else "", "reason_code": reason,
            "event_id": event_id if _EVENT_ID_RE.match(event_id) else ""}, ""


def _replay_key(event: str, lease_id: str) -> str:
    # dlz: recheck P2 — a handoff_ended (an unused link expired) must not eat the Return of a newer
    # link on the same lease, so each event has its own key. One key for every end reason: a second
    # end notice for the lease inside the window needs control's word (below), never the key alone.
    return lease_id if event == "handback" else f"{event}:{lease_id}"


def _seen(key: str, window: float = REPLAY_WINDOW_S) -> bool:
    """True when ``key`` was handled within ``window`` (check only; :func:`_record` records)."""
    now = time.monotonic()
    with _replay_lock:
        for old in [k for k, at in _handled.items() if now - at > max(window, EVENT_RECEIPT_S)]:
            _handled.pop(old, None)
        at = _handled.get(key)
        return at is not None and now - at <= window


def _record(*keys: str) -> None:
    now = time.monotonic()
    with _replay_lock:
        for key in keys:
            if key:
                _handled[key] = now


async def _confirmed_by_control(event_id: str, fields: dict[str, str]) -> Optional[bool]:
    """Control's word on this exact event (no local claim, e.g. after a gateway restart)."""
    if not event_id:
        return False
    import asyncio

    from tools.browser_control_route import confirm_handback_event

    try:
        return await asyncio.to_thread(confirm_handback_event, event_id, fields)
    except Exception:  # dlz: an unreachable control is "retry later", never a wake
        logger.debug("handback confirmation failed", exc_info=True)
        return None


async def inject(runner: Any, session_id: str, slot_id: str, lease_id: str, *, event: str = "handback",
                 reason_code: str = "", event_id: str = "", resumed_lease_id: str = "") -> str:
    """Admit the wake turn. Returns ``"injected"``, ``"replay"``, ``"refused"``, ``"session_not_found"``
    or ``"unavailable"`` (not admitted now: retry).

    The control key is readable by same-uid model code, so the key alone never wakes a session: the
    session must have minted a handoff for this slot + agent lease (or still hold that lease), or
    control must confirm it sent this exact event. Each lease and event wakes at most once per replay
    window on the claim alone; a further notice inside the window needs control's confirmation of a
    new event id. Nothing is recorded or consumed until the turn is admitted."""
    if runner is None:
        return "unavailable"
    from tools.browser_control_route import claim_handback

    # dlz: one admission at a time per claim (session, slot, lease), across events and reasons, so two
    # concurrent requests cannot both spend one minted claim; the loser retries (503).
    claim_key = f"{session_id}|{slot_id}|{lease_id}"
    with _replay_lock:
        if claim_key in _inflight:
            return "unavailable"
        _inflight.add(claim_key)
    try:
        fields = {"event": event, "session_id": session_id, "slot_id": slot_id, "lease_id": lease_id}
        if event == "handoff_ended":
            fields["reason_code"] = reason_code
        else:
            fields["resumed_lease_id"] = resumed_lease_id
        confirmed: Optional[bool] = None
        claimed = claim_handback(session_id, slot_id, lease_id, consume=False)
        if not claimed:
            # dlz: D10-B, fail closed. After a gateway restart the in-memory claim is gone, and a live
            # lease for this task/slot proves nothing about Return. Only control's own outbox, asked
            # for this exact event id, stands in for the claim; never wake on the key alone.
            confirmed = await _confirmed_by_control(event_id, fields)
            if confirmed is None:
                return "unavailable"
            if not confirmed:
                return "refused"
        # Only after the claim (a forged id cannot burn the real one), recorded only once admitted
        # (a failed attempt does not refuse its retry). The event id is control's idempotency key.
        receipt = f"event:{event_id}" if event_id else ""
        if receipt and _seen(receipt, EVENT_RECEIPT_S):
            return "replay"
        key = _replay_key(event, lease_id)
        if _seen(key):
            if confirmed is None and event_id:
                confirmed = await _confirmed_by_control(event_id, fields)
            if confirmed is None and event_id:
                return "unavailable"
            if not confirmed:
                return "replay"
        return await _admit(runner, session_id, slot_id, lease_id, event=event, reason_code=reason_code,
                            keys=(key, receipt))
    finally:
        with _replay_lock:
            _inflight.discard(claim_key)


async def _deliver(runner: Any, session_key: str, content: str) -> bool:
    """dlz: the turn is admitted only when the dispatcher says so (the session FIFO took it, or the
    adapter accepted it). ``_schedule_plugin_message_injection`` answers True before dispatch, and its
    dispatch then refuses a draining gateway, so a 202 there could still mean "never woken"."""
    import asyncio

    dispatch = getattr(runner, "_dispatch_plugin_message_injection", None)
    if dispatch is None:
        schedule = getattr(runner, "_schedule_plugin_message_injection", None)
        return bool(schedule and schedule(content=content, plugin_id=PLUGIN_ID, session_key=session_key))
    accepting = getattr(runner, "_plugin_injection_accepting", None)
    if accepting is not None and not accepting():
        return False
    # Shielded: a cancelled request (gateway shutdown) does not cancel a half-done admission.
    task = asyncio.ensure_future(dispatch(session_key=session_key, content=content, plugin_id=PLUGIN_ID))
    try:
        return bool(await asyncio.shield(task))
    except Exception:  # dlz: a dispatcher fault is "not admitted", logged; control retries
        logger.warning("browser handback dispatch failed", exc_info=True)
        return False


async def _admit(runner: Any, session_id: str, slot_id: str, lease_id: str, *, event: str,
                 reason_code: str, keys: tuple[str, ...]) -> str:
    from tools.browser_control_route import claim_handback

    store = getattr(runner, "async_session_store", None)
    entry = None
    if store is not None and hasattr(store, "lookup_by_session_id"):
        entry = await store.lookup_by_session_id(session_id)
    elif getattr(runner, "session_store", None) is not None:
        entry = runner.session_store.lookup_by_session_id(session_id)
    if entry is None or not getattr(entry, "session_key", ""):
        return "session_not_found"
    if event == "handoff_ended" and reason_code == "expired_unused":
        content = EXPIRED_UNUSED_TEXT.format(slot=slot_id)
    elif event == "handoff_ended":
        why = HANDOFF_ENDED_REASONS.get(reason_code) or HANDOFF_ENDED_REASONS["other"]
        content = HANDOFF_ENDED_TEXT.format(slot=slot_id, why=why)
    else:
        content = HANDBACK_TEXT.format(slot=slot_id)
    if not await _deliver(runner, entry.session_key, content):
        return "unavailable"  # dlz: not admitted; the claim and the markers stay untouched for the retry
    _record(*keys)
    claim_handback(session_id, slot_id, lease_id)  # consume a minted handoff now it is delivered
    if event == "handback":
        from tools.browser_exec_dialogs import note_handoff

        note_handoff(session_id)  # the next exec holds beforeunload: the page may hold Danil's unsaved input
    logger.info("browser %s wake admitted: slot=%s session=%s reason=%s", event, slot_id, session_id,
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
                           event=fields["event"], reason_code=fields["reason_code"],
                           event_id=fields["event_id"], resumed_lease_id=fields["resumed_lease_id"])
    if outcome == "replay":
        return _error("This handback was already delivered.", 409, "handback_replay")
    if outcome == "refused":
        # dlz: D10-B — loud, not silent: Sera is never woken; control shows it on the slot board.
        logger.warning("browser %s NOT delivered: no local handoff claim and control did not confirm "
                       "the event slot=%s session=%s",
                       fields["event"], fields["slot_id"], fields["session_id"])
        return _error("No handoff claim for that session, slot and lease.", 403, "handback_refused")
    if outcome == "session_not_found":
        logger.warning("browser %s NOT delivered: no such session slot=%s session=%s",
                       fields["event"], fields["slot_id"], fields["session_id"])
        return _error("No live session with that id.", 404, "session_not_found")
    if outcome != "injected":
        return _error("Gateway cannot route the wake turn now; retry.", 503, "handback_unavailable")
    return web.json_response({"injected": True}, status=202)


def _http_routes(adapter: Any) -> list[tuple]:
    return [("POST", HANDBACK_PATH, partial(handle_request, adapter))]
