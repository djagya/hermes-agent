"""J-0001 (option c): the turn's own teardown stops the task's CDP supervisor, so its socket to the browser
lease ends with the turn and browser-control can let the lease go idle. A real local WebSocket server stands
in for browser-control's splice: "the lease is free" here means it saw this process's socket end. The close
is injected to fail, to hang and to never run (a blocked supervisor loop); none of them may leave it open."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
import websockets
from websockets.asyncio.server import serve

from agent import chat_completion_helpers as helpers
from tools import browser_payment_done as pay_done
from tools import browser_payment_surface as pay_surface
from tools import browser_supervisor as bs
from tools import browser_session_lifecycle as lifecycle

TASK = "turn-end-task"


UNLOCKED = {"locked": False, "native": False, "tabs": [], "unverified": False}


class _Control:
    """A WebSocket endpoint in its own thread that tracks the connections it has open. It answers the payment
    claim as browser-control does (confirmed), the lock status as unlocked, and anything else with ``{}``."""

    def __init__(self):
        self.open = 0
        self.messages = 0
        self.methods = []
        self.ended = threading.Event()
        self.seen = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()
        assert self._ready.wait(5)

    def _main(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        async def handler(ws):
            self.open += 1
            self.seen.set()
            try:
                async for raw in ws:
                    self.messages += 1
                    msg = json.loads(raw)
                    method, params = msg.get("method"), msg.get("params") or {}
                    self.methods.append(method)
                    result = ({"claimed": True, "locked": True, "tabs": params.get("tabs")}
                              if method == pay_surface.CLAIM_METHOD else
                              UNLOCKED if method == pay_surface.STATUS_METHOD else {})
                    await ws.send(json.dumps({"id": msg.get("id"), "result": result}))
            except Exception:
                pass
            finally:
                self.open -= 1
                self.ended.set()

        self._stop = asyncio.Event()
        async with serve(handler, "127.0.0.1", 0) as server:
            self.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/devtools/browser/x"
            self._ready.set()
            await self._stop.wait()

    def close(self):
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)


@pytest.fixture
def control():
    c = _Control()
    yield c
    c.close()


@pytest.fixture
def supervisor(control, monkeypatch):
    async def _none(*_a, **_k):
        return None

    for name in ("_attach_initial_page", "_rewatch_dialog_tabs", "_payment_recovery"):
        monkeypatch.setattr(bs.CDPSupervisor, name, _none)
    sup = bs.SUPERVISOR_REGISTRY.get_or_start(task_id=TASK, cdp_url=control.url)
    assert control.seen.wait(5) and control.open == 1
    yield sup
    sup._stop_requested = True
    bs.SUPERVISOR_REGISTRY.stop(TASK)


def _turn_end(monkeypatch, *, vm_fails=False):
    """The turn's teardown, as the finalizer runs it (non-headed)."""
    import run_agent

    def vm(_task):
        if vm_fails:
            raise RuntimeError("VM cleanup failed")

    monkeypatch.setattr(run_agent, "cleanup_vm", vm)
    monkeypatch.setattr("agent.chat_completion_helpers.is_persistent_env", lambda _t: False)
    monkeypatch.setattr("tools.browser_session_cloud.is_headed_mode", lambda: False)
    helpers.cleanup_task_resources(SimpleNamespace(verbose_logging=False), TASK)


def _inspected():
    docs = (SimpleNamespace(frame_id="CARD", frame_session="S-card", origin="https://psp.test"),)
    tree = (("PAGE", "LP", "https://shop.test/", "https://shop.test", ""),
            ("CARD", "L1", "https://psp.test/f", "https://psp.test", "PAGE"))
    return SimpleNamespace(documents=docs, page_session="S-page", page_id="PAGE", merchant_origin="https://shop.test",
                           tree=tree, child_sessions={"CARD": "S-card"})


def _freed(control, supervisor, within):
    assert control.ended.wait(within), "browser-control still sees the supervisor's socket"
    assert control.open == 0
    assert bs.SUPERVISOR_REGISTRY.get(TASK) is None


def test_turn_end_stops_the_supervisor_and_frees_its_socket(control, supervisor, monkeypatch):
    _turn_end(monkeypatch, vm_fails=True)  # an earlier teardown step failing does not skip it
    _freed(control, supervisor, 5)
    assert not supervisor._thread.is_alive()


def test_a_failing_session_close_does_not_skip_the_supervisor(control, supervisor, monkeypatch):
    from tools import browser_session as session

    monkeypatch.setitem(session._active_sessions, f"{TASK}@home", {"session_name": "s"})

    def boom(_key):
        raise RuntimeError("backend teardown failed")

    monkeypatch.setattr(lifecycle, "cleanup_single_session", boom)
    with pytest.raises(RuntimeError):
        lifecycle.cleanup_browser(TASK)
    _freed(control, supervisor, 5)


def test_a_close_that_raises_still_frees_the_socket(control, supervisor, monkeypatch):
    async def fails():
        raise OSError("close frame could not be sent")

    monkeypatch.setattr(supervisor._ws, "close", fails)
    _turn_end(monkeypatch)
    _freed(control, supervisor, 5)


def test_a_close_that_hangs_still_frees_the_socket(control, supervisor, monkeypatch):
    async def hangs():
        await asyncio.Event().wait()

    monkeypatch.setattr(supervisor._ws, "close", hangs)
    started = time.monotonic()
    _turn_end(monkeypatch)
    _freed(control, supervisor, 5)
    assert time.monotonic() - started < 7


def test_a_blocked_loop_that_never_runs_the_close_still_frees_the_socket(control, supervisor, monkeypatch):
    """The close times out before it starts (the loop is stuck in a sync call): stop() shuts the socket down
    from the caller's thread while the loop is still blocked."""
    release = threading.Event()
    supervisor._loop.call_soon_threadsafe(release.wait, 30)
    try:
        _turn_end(monkeypatch)
        assert control.ended.wait(5), "browser-control still sees the supervisor's socket"
        assert control.open == 0 and not release.is_set()
        assert bs.SUPERVISOR_REGISTRY.get(TASK) is None
    finally:
        release.set()
    supervisor._thread.join(5)
    assert not supervisor._thread.is_alive()


def test_a_stop_during_the_dial_closes_the_socket_it_returns(control, monkeypatch):
    """stop() while the dial is in flight sees no socket to close; the socket the dial returns is closed when it
    lands, before anything is sent on it."""
    real_connect = websockets.connect
    landed = threading.Event()
    sup = bs.CDPSupervisor(task_id=TASK, cdp_url=control.url)

    async def slow_connect(*a, **k):
        await asyncio.sleep(0.3)
        sup._stop_requested = True  # stop() ran meanwhile
        ws = await real_connect(*a, **k)
        landed.set()
        return ws

    monkeypatch.setattr(websockets, "connect", slow_connect)
    sup._ready_event.set()  # past the first attach: a redial
    asyncio.run(asyncio.wait_for(sup._run(), timeout=5))
    assert landed.is_set() and control.ended.wait(5) and control.open == 0
    assert control.messages == 0


@pytest.mark.parametrize("held", ["locked", "payment"])
def test_a_supervisor_in_a_card_payment_survives_the_turn_end(control, supervisor, monkeypatch, held):
    """The lock's native socket is the only way out of a payment: the turn end leaves it to its lease."""
    if held == "locked":
        monkeypatch.setattr(pay_surface, "locally_locked", lambda s: s is supervisor)
    else:
        monkeypatch.setattr(pay_done, "has_payment", lambda s: s is supervisor)
    _turn_end(monkeypatch)
    assert bs.SUPERVISOR_REGISTRY.get(TASK) is supervisor
    assert not control.ended.wait(0.5) and control.open == 1


def test_a_claim_admitted_at_turn_end_keeps_the_supervisor(control, supervisor, monkeypatch):
    """A claim that holds the claim lock but has not marked the lock yet: the turn end keeps the supervisor, and the
    claim then commits on a live socket."""
    lock = pay_surface._claim_lock(supervisor)
    lock.acquire()
    try:
        _turn_end(monkeypatch)
    finally:
        lock.release()
    assert pay_surface.claim_payment_surface(supervisor, _inspected()) is None
    assert bs.SUPERVISOR_REGISTRY.get(TASK) is supervisor
    assert not control.ended.wait(0.5) and control.open == 1


def test_a_claim_after_the_stop_decision_refuses_and_sends_nothing(control, supervisor, monkeypatch):
    """A claim (an abandoned tool worker) that starts after the turn end decided to stop the supervisor: refused
    before anything is sent, so no claim is committed on a socket that is about to close."""
    real_stop, answers = supervisor.stop, []

    def claim_then_stop(*a, **k):
        answers.append(pay_surface.claim_payment_surface(supervisor, _inspected()))
        real_stop(*a, **k)

    monkeypatch.setattr(supervisor, "stop", claim_then_stop)
    _turn_end(monkeypatch)
    assert [a and a.get("error_type") for a in answers] == [pay_surface.UNAVAILABLE]
    assert pay_surface.CLAIM_METHOD not in control.methods
    _freed(control, supervisor, 5)


def test_a_replacement_registered_after_the_check_is_not_stopped(control, supervisor, monkeypatch):
    """A supervisor that replaced the checked one before the stop keeps its socket; only the checked one stops."""
    replacement = bs.CDPSupervisor(task_id=TASK, cdp_url=control.url)
    replacement.start()
    try:
        assert control.open == 2

        def replaced(_sup):
            with bs.SUPERVISOR_REGISTRY._lock:  # as get_or_start registers it
                bs.SUPERVISOR_REGISTRY._by_task[TASK] = replacement
            return False

        monkeypatch.setattr(pay_done, "has_payment", replaced)
        _turn_end(monkeypatch)
        assert control.ended.wait(5) and control.open == 1
        assert not supervisor._thread.is_alive() and replacement._thread.is_alive()
        assert bs.SUPERVISOR_REGISTRY.get(TASK) is replacement
    finally:
        replacement.stop()


def test_a_pending_dialog_survives_the_turn_end_and_is_answered_next_turn(control, supervisor, monkeypatch):
    """The user is asked between turns (must_respond, 300 s): the supervisor holding the dialog is kept, the next
    turn answers it, and the turn end after that stops it."""
    from tools.browser_dialog_tool import browser_dialog

    asyncio.run_coroutine_threadsafe(
        supervisor._on_dialog_opening({"type": "confirm", "message": "Place order?"}, None), supervisor._loop
    ).result(5)
    assert len(supervisor.snapshot().pending_dialogs) == 1
    _turn_end(monkeypatch)
    assert bs.SUPERVISOR_REGISTRY.get(TASK) is supervisor and control.open == 1
    out = json.loads(browser_dialog("accept", task_id=TASK))
    assert out["success"] is True and out["dialog"]["message"] == "Place order?"
    assert "Page.handleJavaScriptDialog" in control.methods
    _turn_end(monkeypatch)
    _freed(control, supervisor, 5)
