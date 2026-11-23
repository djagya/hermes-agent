"""J-0001 (option c): the turn's own teardown stops the task's CDP supervisor, so its socket to the browser
lease ends with the turn and browser-control can let the lease go idle. A real local WebSocket server stands
in for browser-control's splice: "the lease is free" here means it saw this process's socket end. The close
is injected to fail, to hang and to never run (a blocked supervisor loop); none of them may leave it open."""

from __future__ import annotations

import asyncio
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


class _Control:
    """A WebSocket endpoint in its own thread that tracks the connections it has open."""

    def __init__(self):
        self.open = 0
        self.messages = 0
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
                async for _ in ws:
                    self.messages += 1
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
