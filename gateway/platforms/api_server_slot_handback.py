"""Wake a session when a human returns its managed browser slot.

Monolith browser-control hands a slot to a human (handoff link or the slot
board) and back again with **Return to Sera**. Until now the agent only learned
of the return when the human also typed in chat. Control now calls
``POST /v1/browser-slots/handback`` on this API server with the same bearer key
the agent uses for control (``HERMES_BROWSER_CONTROL_KEY`` or
``HERMES_BROWSER_CONTROL_KEY_FILE``), naming the session id that holds the
lease, the agent lease that was handed off and the ``event_id`` of the event
in control's outbox. The control key is readable by same-uid code, so neither
the key nor a handoff this process minted is enough: the gateway wakes the session only when control confirms, for that very request, that it sent this
exact event and still holds it undelivered (``GET /v1/handback/pending?event_id=``; control's outbox
lives in another container). It injects one internal turn whose text it composes itself: nothing
from the request body reaches the model except a validated slot id.

dlz: durable delivery (Codex qol #10, recheck P1/P2). Control keeps each event in an outbox and
retries transient failures with the same ``event_id``. That id is the session FIFO's admission id
(``handback:<event_id>``), so a retry after a lost 202 — even across a gateway restart — is the same
admission, not a second wake; an in-process receipt answers the common retry without asking control.
Nothing is recorded until the turn is admitted, so a 503 ("not admitted, retry") never burns the
wake. The authorized session id travels into the dispatch: a session key that rotated to another
conversation (/new) is refused, never redirected. Answers: 202 admitted, 409 already handled, 503
not admitted now (retry), 403 refused (control does not confirm), 404 no such (live) session.

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
from pathlib import Path
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


def _seen(key: str, window: float = EVENT_RECEIPT_S) -> bool:
    """True when ``key`` was admitted within ``window`` (check only; :func:`_record` records)."""
    now = time.monotonic()
    with _replay_lock:
        for old in [k for k, at in _handled.items() if now - at > EVENT_RECEIPT_S]:
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
    """Control's word on this exact event: True / False, None when it did not answer in time."""
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

    dlz: Codex recheck P1. The control key is readable by same-uid model code, so neither the key
    nor a local handoff claim ever wakes a session: control must confirm, for every event, that it
    sent this exact event (``GET /v1/handback/pending``) and still holds it undelivered. No
    ``event_id`` means nothing control can confirm: refused. The confirmation, the dispatch, the
    receipt and the claim consumption run in one shielded task that releases the in-flight marker
    itself, so a cancelled request never leaves an admission without its receipt (recheck P2)."""
    if runner is None:
        return "unavailable"
    if not event_id:
        return "refused"
    import asyncio

    receipt = f"event:{event_id}"
    if _seen(receipt):
        return "replay"
    # dlz: one admission at a time per (session, slot, lease), across events and reasons; the loser
    # retries (503). Released by the owner task when it finishes, never by a cancelled caller.
    claim_key = f"{session_id}|{slot_id}|{lease_id}"
    with _replay_lock:
        if claim_key in _inflight:
            return "unavailable"
        _inflight.add(claim_key)
    try:
        task = asyncio.ensure_future(_owned(runner, session_id, slot_id, lease_id, event=event,
                                            reason_code=reason_code, event_id=event_id,
                                            resumed_lease_id=resumed_lease_id, claim_key=claim_key))
    except BaseException:
        with _replay_lock:
            _inflight.discard(claim_key)
        raise
    _owners.add(task)
    task.add_done_callback(_owners.discard)
    return await asyncio.shield(task)


# Strong references to running owner tasks (a shielded task outlives its cancelled caller).
_owners: set[Any] = set()


async def _owned(runner: Any, session_id: str, slot_id: str, lease_id: str, *, event: str, reason_code: str,
                 event_id: str, resumed_lease_id: str, claim_key: str) -> str:
    try:
        fields = {"event": event, "session_id": session_id, "slot_id": slot_id, "lease_id": lease_id}
        if event == "handoff_ended":
            fields["reason_code"] = reason_code
        else:
            fields["resumed_lease_id"] = resumed_lease_id
        receipt = f"event:{event_id}"
        if _seen(receipt):
            return "replay"
        confirmed = await _confirmed_by_control(event_id, fields)
        if confirmed is None:
            return "unavailable"
        if not confirmed:
            return "refused"
        return await _admit(runner, session_id, slot_id, lease_id, event=event, reason_code=reason_code,
                            event_id=event_id)
    except Exception:  # dlz: an unexpected fault is "not admitted"; control retries with the same id
        logger.warning("browser handback admission failed", exc_info=True)
        return "unavailable"
    finally:
        with _replay_lock:
            _inflight.discard(claim_key)


def _owning_home(runner: Any, session_key: str) -> Optional[Path]:
    """dlz: recheck P2 (local sessions). The profile home whose session authority holds a live
    session on ``session_key`` (a CLI/TUI/Desktop session is woken only through it), else None."""
    from gateway.session_authorities import all_authorities

    for authority in all_authorities(runner):
        sessions = getattr(authority, "sessions", None) or {}
        if any(getattr(live, "route", None) == session_key for live in list(sessions.values())):
            return Path(str(authority.profile_id))
    return None


def _is_local(entry: Any) -> bool:
    platform = getattr(getattr(entry, "origin", None), "platform", None)
    return getattr(platform, "value", platform) == "local"


async def _deliver(runner: Any, session_key: str, content: str, *, session_id: str, event_id: str,
                   plugin_home: Optional[Path]) -> bool:
    """dlz: the turn is admitted only when the dispatcher says so (the session FIFO took it, or the
    adapter accepted it). ``_schedule_plugin_message_injection`` answers True before dispatch, and its
    dispatch then refuses a draining gateway, so a 202 there could still mean "never woken". The
    dispatcher gets the authorized session id (a rotated key is refused, not redirected) and the
    event id as the FIFO admission id (a replay after a restart is the same admission)."""
    dispatch = getattr(runner, "_dispatch_plugin_message_injection", None)
    if dispatch is None:
        schedule = getattr(runner, "_schedule_plugin_message_injection", None)
        return bool(schedule and schedule(content=content, plugin_id=PLUGIN_ID, session_key=session_key))
    accepting = getattr(runner, "_plugin_injection_accepting", None)
    if accepting is not None and not accepting():
        return False
    try:
        return bool(await dispatch(session_key=session_key, content=content, plugin_id=PLUGIN_ID,
                                   plugin_home=plugin_home, expected_session_id=session_id,
                                   admission_id=f"handback:{event_id}"))
    except Exception:  # dlz: a dispatcher fault is "not admitted", logged; control retries
        logger.warning("browser handback dispatch failed", exc_info=True)
        return False


async def _admit(runner: Any, session_id: str, slot_id: str, lease_id: str, *, event: str,
                 reason_code: str, event_id: str) -> str:
    from tools.browser_control_route import claim_handback

    store = getattr(runner, "async_session_store", None)
    entry = None
    if store is not None and hasattr(store, "lookup_by_session_id"):
        entry = await store.lookup_by_session_id(session_id)
    elif getattr(runner, "session_store", None) is not None:
        entry = runner.session_store.lookup_by_session_id(session_id)
    if entry is None or not getattr(entry, "session_key", ""):
        return "session_not_found"
    # Only a local session needs its owning profile named (a messaging session is checked by its key).
    home = _owning_home(runner, entry.session_key) if _is_local(entry) else None
    if home is None and _is_local(entry):
        # dlz: a local session with no live owner (its CLI/TUI/Desktop closed) cannot be woken:
        # say so once (404, shown on the board) instead of 30 minutes of retries.
        logger.warning("browser %s NOT delivered: local session %s is not live", event, session_id)
        return "session_not_found"
    if event == "handoff_ended" and reason_code == "expired_unused":
        content = EXPIRED_UNUSED_TEXT.format(slot=slot_id)
    elif event == "handoff_ended":
        why = HANDOFF_ENDED_REASONS.get(reason_code) or HANDOFF_ENDED_REASONS["other"]
        content = HANDOFF_ENDED_TEXT.format(slot=slot_id, why=why)
    else:
        content = HANDBACK_TEXT.format(slot=slot_id)
    if not await _deliver(runner, entry.session_key, content, session_id=session_id, event_id=event_id,
                          plugin_home=home):
        return "unavailable"  # dlz: not admitted; nothing recorded or consumed for the retry
    _record(f"event:{event_id}")
    claim_handback(session_id, slot_id, lease_id)  # forget a minted handoff now it is delivered
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
        logger.warning("browser %s NOT delivered: control did not confirm the event slot=%s session=%s",
                       fields["event"], fields["slot_id"], fields["session_id"])
        return _error("Control did not confirm that event.", 403, "handback_refused")
    if outcome == "session_not_found":
        logger.warning("browser %s NOT delivered: no such session slot=%s session=%s",
                       fields["event"], fields["slot_id"], fields["session_id"])
        return _error("No live session with that id.", 404, "session_not_found")
    if outcome != "injected":
        return _error("Gateway cannot route the wake turn now; retry.", 503, "handback_unavailable")
    return web.json_response({"injected": True}, status=202)


def _http_routes(adapter: Any) -> list[tuple]:
    return [("POST", HANDBACK_PATH, partial(handle_request, adapter))]
