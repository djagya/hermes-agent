"""Browser fault hunt 2026-11-07, group G4 (browser_exec + supervisor): B14, B15, B16, B23, B25, timeouts."""

import asyncio
import json
import time
from pathlib import Path

import pytest

import tools.browser_control_route as route
import tools.browser_supervisor as sup_mod
import tools.browser_use_cli as bu_cli
from tests.tools.test_browser_control_route import DEFAULT_LEASE, _fake_cli, _managed_env, _start, _State
from tools.browser_control_route import clear_held_leases

CONTRACT = Path(__file__).resolve().parents[1] / "fixtures" / "browser_control" / "control-v1.json"


@pytest.fixture(autouse=True)
def _clean():
    clear_held_leases()
    getattr(bu_cli, "_attach_failures", {}).clear()
    yield
    clear_held_leases()
    getattr(bu_cli, "_attach_failures", {}).clear()
    sup_mod.SUPERVISOR_REGISTRY.stop_all()


def test_handshake_timeout_is_explained_and_retried_once(tmp_path, monkeypatch):
    """B16: control could not reach the slot's Chrome; no popup advice, one retry on the same live lease."""
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda env, task_id: None)
        calls = tmp_path / "calls"
        script = _fake_cli(
            tmp_path,
            'case "$1" in --reload) exit 0;; esac\n'
            f"echo x >> '{calls}'\n"
            f"if [ $(wc -l < '{calls}') -eq 1 ] || [ -f '{tmp_path}/always' ]; then\n"
            '  echo "CDP WS handshake failed: timed out during opening handshake -- click Allow in Chrome '
            'if prompted" >&2; exit 1\nfi\n'
            "echo ok\n",
        )
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(script)])
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t1"))
        assert result["success"] is True
        assert len(calls.read_text().split()) == 2
        (tmp_path / "always").write_text("1")
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t1"))
        assert result["success"] is False
        assert result["stderr"].startswith("the browser slot did not answer")
        assert "click Allow" not in result["stderr"]
    finally:
        httpd.shutdown()


def test_qa_targets_schema_names_the_registry_not_hostnames(monkeypatch):
    """B23: control admits registered app names only (grant.py admit)."""
    monkeypatch.setattr(route, "is_managed", lambda: True)
    text = bu_cli._dynamic_schema_overrides()["parameters"]["properties"]["qa_targets"]["description"]
    assert "Registered QA app names" in text and "not hostnames" in text and "mode=research" in text


class _HungTabs(sup_mod.CDPSupervisor):
    """Existing tabs answer attach and two enables just inside each call's timeout, then hang."""

    def __init__(self):
        super().__init__("t-hung", "ws://127.0.0.1:1/devtools/browser/x")
        self.calls = []

    async def _cdp(self, method, params=None, *, session_id=None, timeout=10.0):
        self.calls.append(method)
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [{"type": "page", "targetId": "A"}, {"type": "page", "targetId": "B"}]}}
        if method == "Target.createTarget":
            return {"result": {"targetId": "NEW"}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "s-" + params["targetId"]}}
        if session_id in {"s-A", "s-B"}:
            if method == "Target.setAutoAttach":
                await asyncio.sleep(timeout)
                raise asyncio.TimeoutError()
            await asyncio.sleep(timeout * 0.9)
        return {"result": {}}

    async def _install_dialog_bridge(self, sid):
        return None


def test_initial_attach_fits_the_start_budget_with_hung_tabs(monkeypatch):
    """B15: two hung tabs used to eat the whole start budget before the createTarget fallback ran."""
    monkeypatch.setattr(sup_mod, "PAGE_ATTACH_TIMEOUT_S", 0.4)
    monkeypatch.setattr(sup_mod, "CREATE_TARGET_RESERVE_S", 0.3, raising=False)
    sup = _HungTabs()
    sup._attach_deadline = time.monotonic() + 1.4
    started = time.monotonic()
    asyncio.run(sup._attach_initial_page())
    assert time.monotonic() - started < 1.5
    assert sup._page_session_id == "s-NEW"


def test_failed_attach_is_cached_typed_and_reported(tmp_path, monkeypatch):
    """B15: a failed attach is not re-paid on every exec, logs its type, and the exec result says so."""
    attempts = []

    def _fail(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError()

    monkeypatch.setattr(sup_mod.SUPERVISOR_REGISTRY, "get_or_start", _fail)
    monkeypatch.setattr("tools.browser_session_cdp.resolve_cdp_override", lambda url: url)
    env = {"BU_CDP_URL": "http://ctl/slot/s/json/version?tok=t", "BU_NAME": "bu-ctrl-s-g-1"}
    bu_cli._attach_vault_supervisor(env, "t1")
    bu_cli._attach_vault_supervisor(env, "t1")
    assert len(attempts) == 1
    assert bu_cli.vault_route("t1")["error"].startswith("TimeoutError")

    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        script = _fake_cli(tmp_path, 'case "$1" in --reload) exit 0;; esac\necho ok\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(script)])
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t1"))
        assert result["success"] is True
        assert result["supervisor"].startswith("unavailable")
    finally:
        httpd.shutdown()


def test_gate_403_on_redial_is_final(monkeypatch):
    """B14: a 403 on the WebSocket upgrade means the lease ended; no 5-try redial loop of a dead lease."""
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    dials = []

    async def _connect(*args, **kwargs):
        dials.append(1)
        raise InvalidStatus(Response(403, "Forbidden", Headers()))

    async def _no_sleep(_s):
        return None

    import websockets

    monkeypatch.setattr(websockets, "connect", _connect)
    monkeypatch.setattr(sup_mod.asyncio, "sleep", _no_sleep)
    sup = sup_mod.CDPSupervisor("t-403", "ws://127.0.0.1:1/devtools/browser/x")
    sup._ready_event.set()  # attached once before: the redial path
    sup_mod.SUPERVISOR_REGISTRY._by_task["t-403"] = sup
    asyncio.run(sup._run())
    assert dials == [1]
    assert sup_mod.SUPERVISOR_REGISTRY.get("t-403") is None


def test_released_lease_stops_its_supervisor(monkeypatch):
    """B14: the lease's supervisor stops with its daemon, instead of redialing the dead lease."""
    stopped = []

    class _Sup:
        cdp_url = "ws://ctl/devtools/browser/x?tok=t"

        def stop(self):
            stopped.append(1)

    sup_mod.SUPERVISOR_REGISTRY._by_task["t-rel"] = _Sup()
    bu_cli._VAULT_ROUTES["t-rel"] = {"bu_name": "bu-ctrl-s-g-1", "supervisor_url": _Sup.cdp_url}
    monkeypatch.setattr(bu_cli, "_reload_daemons", lambda daemons: None)
    try:
        bu_cli.stop_harness_daemon("bu-ctrl-other-g-1")
        assert stopped == []
        bu_cli.stop_harness_daemon("bu-ctrl-s-g-1")
        assert stopped == [1] and sup_mod.SUPERVISOR_REGISTRY.get("t-rel") is None
    finally:
        bu_cli._VAULT_ROUTES.pop("t-rel", None)


def test_evaluate_timeout_after_renderer_oom_says_so(tmp_path, monkeypatch):
    """B25: control saw an oom_kill on the slot during the call; Sera is told, not just "timed out"."""
    state = _State()
    httpd, port = _start(state)
    try:
        _managed_env(monkeypatch, port)
        monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda env, task_id: None)
        script = _fake_cli(
            tmp_path,
            'case "$1" in --reload) exit 0;; esac\n'
            'echo "RuntimeError: Runtime.evaluate timed out after 30s" >&2; exit 1\n',
        )
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(script)])
        oom_at = {"research-1": 0.0}
        monkeypatch.setattr(route, "slot_status", lambda slot: {"last_oom_at": oom_at[slot]})
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t1"))
        assert result["stderr"].startswith("RuntimeError")
        oom_at["research-1"] = time.time() + 1.0
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t1"))
        assert result["stderr"].startswith("the browser slot's Chrome was killed for memory (OOM)")
    finally:
        httpd.shutdown()


def test_fork_timeouts_match_the_control_contract():
    """Fork half of the shared timeout table (fault-hunt B5): discovery budget < client read < start."""
    from tools.browser_session_cdp import CDP_CLIENT_READ_TIMEOUT_S

    timeouts = json.loads(CONTRACT.read_text(encoding="utf-8"))["timeouts"]
    assert CDP_CLIENT_READ_TIMEOUT_S == timeouts["cdp_client_read_timeout_s"]
    assert sup_mod.CDP_CLIENT_TIMEOUT_S == timeouts["cdp_client_read_timeout_s"]
    assert sup_mod.SUPERVISOR_START_TIMEOUT_S == timeouts["cdp_supervisor_start_s"]
    assert timeouts["cdp_discovery_budget_s"] < CDP_CLIENT_READ_TIMEOUT_S < sup_mod.SUPERVISOR_START_TIMEOUT_S
    assert sup_mod.CDPSupervisor.start.__defaults__ == (sup_mod.SUPERVISOR_START_TIMEOUT_S,)
