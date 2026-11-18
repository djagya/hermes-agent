"""Build C round 2 (P5/P6, Codex P2, Opus LOW3/LOW4): the real CDPSupervisor ``_run`` loop on a fake browser-control
socket, reached through the vault tool's entry (``_payment_surface_action``). After a known native-socket change the
supervisor serves the lock status-only until control proves an unlock (an unanswered or malformed status never makes
it attach), then attaches as usual; a fresh supervisor on a slot another connection locked starts without attaching
and answers ``payment_lock_foreign``; a dead endpoint still spends the reconnect budget, never in a 0.5 s loop.
No real Chrome: ``websockets.connect`` is replaced and the attach itself is recorded, not performed."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from tests.tools.test_browser_payment_surface import _inspected
from tests.waiting import wait_until
from tools import browser_payment_surface as surface
from tools import browser_supervisor as bs
from tools import browser_vault_tool as vault_tool

CDP = "ws://127.0.0.1:9/devtools/browser/fake"


class FakeWS:
    """One CDP socket to the fake control: replies are queued for the supervisor's read loop."""

    def __init__(self, control):
        self.control, self.queue, self.closed = control, asyncio.Queue(), False
        self.loop = asyncio.get_running_loop()

    async def send(self, raw):
        msg = json.loads(raw)
        reply = self.control.handle(self, msg)
        if reply is not None:
            await self.queue.put(json.dumps(reply))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.closed = True
        self.queue.put_nowait(None)

    def drop(self):
        """Control (or the network) ends this socket, from any thread."""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, None)


class FakeControl:
    """browser-control's lock as seen over CDP: the claiming socket is native; status says so per socket."""

    def __init__(self):
        self.sockets, self.lock, self.native, self.mode = [], set(), None, "answer"
        self.status_calls, self.drop_new = [], False

    async def connect(self, *_args, **_kwargs):
        ws = FakeWS(self)
        self.sockets.append(ws)
        if self.drop_new:
            ws.queue.put_nowait(None)
        return ws

    def handle(self, ws, msg):
        method, out = msg.get("method"), {}
        if method == surface.CLAIM_METHOD:
            self.lock, self.native = set(msg["params"]["tabs"]), ws
            out = {"claimed": True, "locked": True, "tabs": sorted(self.lock)}
        elif method == surface.STATUS_METHOD:
            self.status_calls.append(ws)
            if self.mode == "malformed":
                out = {"bogus": True}
            else:
                locked = bool(self.lock)
                out = {"locked": locked, "native": locked and ws is self.native, "tabs": sorted(self.lock),
                       "unverified": False}
        return {"id": msg["id"], "result": out}


@pytest.fixture
def control(monkeypatch):
    """The fake control behind the vault tool's managed route; every attach recorded with its socket."""
    ctl = FakeControl()
    ctl.attaches = []

    async def attach(self):
        ctl.attaches.append(self._ws)
        self._page_session_id = "S-page"

    async def noop(self):
        return None

    monkeypatch.setattr(websockets, "connect", ctl.connect)
    monkeypatch.setattr(bs.CDPSupervisor, "_attach_initial_page", attach)
    monkeypatch.setattr(bs.CDPSupervisor, "_rewatch_dialog_tabs", noop)
    monkeypatch.setattr(bs, "PAYMENT_RECOVERY_POLL_S", 0.05)
    monkeypatch.setattr(vault_tool, "_is_managed", lambda: True)
    monkeypatch.setattr("tools.browser_use_cli.vault_route", lambda task_id: {"lease": "l", "cdp": CDP, "token": "t"})
    monkeypatch.setattr("tools.browser_control_route._lease_alive", lambda url, lease: True)
    monkeypatch.setattr("tools.browser_control_route.control_url", lambda: "http://control.invalid")
    monkeypatch.setattr("tools.browser_session_cdp.resolve_cdp_override", lambda url: url)
    monkeypatch.setattr("tools.browser_session_cdp.get_dialog_policy_config",
                        lambda: (bs.DEFAULT_DIALOG_POLICY, bs.DEFAULT_DIALOG_TIMEOUT_S))
    tasks = []
    yield ctl, tasks
    for task in tasks:
        bs.SUPERVISOR_REGISTRY.stop(task)


def _action(task):
    return json.loads(vault_tool._payment_surface_action(task, None))


def test_a_lost_native_socket_stays_status_only_until_control_proves_the_unlock(control):
    ctl, tasks = control
    tasks.append("t-lost")
    sup = vault_tool._managed_supervisor("t-lost")  # dial 1: no lock, attached
    assert ctl.attaches == [ctl.sockets[0]] and sup.snapshot().active is True
    assert surface.claim_payment_surface(sup, _inspected()) is None  # the card fill's claim on socket 1
    assert ctl.native is ctl.sockets[0]
    ctl.sockets[0].drop()  # the native socket is lost during the lock
    wait_until(lambda: len(ctl.sockets) == 2 and ctl.sockets[1] in ctl.status_calls, what="redial, status only")
    assert _action("t-lost")["error_type"] == surface.NATIVE_LOST, "never supervisor_unavailable"
    ctl.mode = "malformed"  # status unreadable: still no attach
    seen = len(ctl.status_calls)
    wait_until(lambda: len(ctl.status_calls) >= seen + 3, what="status keeps being polled")
    assert ctl.attaches == [ctl.sockets[0]] and len(ctl.sockets) == 2
    assert sup.snapshot().active is False and bs.SUPERVISOR_REGISTRY.get("t-lost") is sup
    ctl.mode, ctl.lock = "answer", set()  # the payment tab closed: control unlocks the slot
    wait_until(lambda: len(ctl.attaches) == 2, what="attach after the proven unlock")
    assert ctl.sockets[1].closed and ctl.attaches[1] is ctl.sockets[2]
    assert sup.snapshot().active is True and surface.claim_socket_changed(sup) is False


def test_a_fresh_supervisor_on_a_foreign_lock_starts_without_attaching(control):
    ctl, tasks = control
    tasks.append("t-foreign")
    ctl.lock, ctl.native = {"PAGE"}, object()  # another connection's lock
    sup = vault_tool._managed_supervisor("t-foreign")  # start() returns: not attached, not active
    assert ctl.attaches == [] and sup.snapshot().active is False
    out = _action("t-foreign")
    assert out["error_type"] == surface.LOCK_FOREIGN and "unless Danil says so" in out["error"]
    ctl.lock = set()
    wait_until(lambda: ctl.attaches, what="attach once the foreign lock ended")
    assert ctl.attaches == [ctl.sockets[1]] and sup.snapshot().active is True


def test_status_only_recovery_spends_the_budget_without_a_fast_loop(monkeypatch):
    """A socket control ends at once, with no status answered: each one counts against the budget and the redial
    waits at least the status poll interval, so a dead endpoint never loops at 0.5 s and still ends."""
    ctl = FakeControl()
    ctl.drop_new, ctl.mode = True, "malformed"
    sup = bs.CDPSupervisor(task_id="t-budget", cdp_url=CDP)
    bs.SUPERVISOR_REGISTRY._by_task[sup.task_id] = sup
    sleeps = []
    real_sleep = asyncio.sleep

    async def recorded(delay):
        sleeps.append(delay)
        await real_sleep(0)

    async def unexpected(self):
        raise AssertionError("attached during a lost payment lock")

    monkeypatch.setattr(websockets, "connect", ctl.connect)
    monkeypatch.setattr(bs.CDPSupervisor, "_attach_initial_page", unexpected)
    monkeypatch.setattr(bs.asyncio, "sleep", recorded)
    sup._ready_event.set()  # the first attach happened long ago
    gen = surface._LockGen(known=True)
    gen.owned, gen.ws = frozenset({"PAGE"}), object()  # claimed on a socket that is gone
    with surface._LOCK:
        surface._HELD.add(sup)
        surface._GENS[sup] = gen

    async def main():
        sup._loop = asyncio.get_running_loop()
        await asyncio.wait_for(sup._run(), timeout=30)

    try:
        asyncio.run(main())
        assert len(ctl.sockets) == bs.MAX_POST_ATTACH_RECONNECT_FAILURES
        assert bs.SUPERVISOR_REGISTRY.get("t-budget") is None
        assert sleeps and min(sleeps) >= bs.PAYMENT_RECOVERY_POLL_S
    finally:
        with surface._LOCK:
            surface._HELD.discard(sup)
            surface._GENS.pop(sup, None)
        bs.SUPERVISOR_REGISTRY._pop("t-budget")
