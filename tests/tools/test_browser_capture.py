"""ux_capture helpers for browser_exec, against a fake CDP harness (no Chrome)."""

import base64
import hashlib
import io
import json
import shutil
import subprocess

import pytest
from PIL import Image

from tools import browser_capture, browser_capture_harness as h
from tools import browser_control_route, browser_payment_privacy as guard, browser_use_cli


def _png(color, size=(40, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


class FakeHarness:
    """cdp/js/drain_events as the harness exposes them; the 'page' colour follows the scheme."""

    def __init__(self):
        self.calls, self.events, self.scheme, self.fail_shot = [], [], "light", False
        self.axe_loaded, self.isolated_sources = False, []

    def cdp(self, method, session_id=None, _response_timeout=5.0, **params):
        self.calls.append((method, params))
        if method == "Emulation.setEmulatedMedia":
            feats = {f["name"]: f["value"] for f in params.get("features", [])}
            self.scheme = feats.get("prefers-color-scheme", "light")
        if method == "Page.captureScreenshot":
            if self.fail_shot:
                raise RuntimeError("Page.captureScreenshot timed out")
            return {"data": _png((20, 20, 20) if self.scheme == "dark" else (250, 250, 250))}
        if method == "Page.getLayoutMetrics":
            return {"cssContentSize": {"width": 390, "height": 9000}}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            return self._evaluate(params["expression"])
        if method == "Performance.getMetrics":
            return {"metrics": [{"name": "Nodes", "value": 120}, {"name": "Secret", "value": 1}]}
        return {}

    def _evaluate(self, expr):
        if expr.startswith("globalThis.__hermesAxe +="):
            self.isolated_sources.append(expr)
        if "(0, eval)" in expr:
            self.axe_loaded = True
        if expr.startswith("typeof axe"):
            return {"result": {"value": self.axe_loaded}}
        if "axe.run" in expr:
            out = {"version": "4.14.0", "url": "https://example.test/", "passes": 30, "incomplete": [],
                   "violations": [{"id": "color-contrast", "impact": "serious", "help": "contrast", "nodes": 3,
                                   "helpUrl": "", "targets": []}]}
            return {"result": {"value": json.dumps(out)}}
        return {"result": {"value": None}}

    def js(self, expr):
        if "location.href" in expr and "scrollWidth" in expr:
            return json.dumps({"url": "https://example.test/", "title": "Example", "scroll_width": 390,
                               "client_width": 390})
        if "performance" in expr:
            return json.dumps({"lcp_ms": 3100.0, "cls": 0.02, "inp_ms": None, "fcp_ms": 900.0, "ttfb_ms": 120.0})
        return True

    def drain_events(self):
        out, self.events = self.events, []
        return out

    def globals(self):
        return {"cdp": self.cdp, "js": self.js, "drain_events": self.drain_events, "wait_for_load": lambda t: True}


@pytest.fixture
def cap(tmp_path, monkeypatch):
    fake = FakeHarness()
    monkeypatch.setattr(h.time, "sleep", lambda s: None)
    env = {h.ROOT_ENV: str(tmp_path / "ux"), h.GUARD_ENV: str(guard._state_path()), h.TASK_ENV: "t1",
           h.SLOT_ENV: "research-1", h.GENERATION_ENV: "g1"}
    return fake, h.UXCapture(fake.globals(), env)


def _methods(fake):
    return [m for m, _ in fake.calls]


def test_matrix_writes_verified_pngs_and_restores_emulation_even_on_failure(cap):
    fake, ux = cap
    run = ux.run("home")
    shots = run.matrix("home", viewports=("phone", "desktop", "800x600@2m"), schemes=("light", "dark"))
    assert len(shots) == 6
    for s in shots:
        data = open(s["path"], "rb").read()
        assert data.startswith(h.PNG_MAGIC) and s["bytes"] == len(data)
        assert s["sha256"] == hashlib.sha256(data).hexdigest() and (s["width_px"], s["height_px"]) == (40, 30)
    phone = next(c for m, c in fake.calls if m == "Emulation.setDeviceMetricsOverride")
    assert phone == {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True,
                     "screenWidth": 390, "screenHeight": 844}
    assert _methods(fake)[-3:] == ["Emulation.clearDeviceMetricsOverride", "Emulation.setTouchEmulationEnabled",
                                   "Emulation.setEmulatedMedia"]
    index = json.loads((run.dir / "index.json").read_text(encoding="utf-8-sig"))
    assert [e["seq"] for e in index["entries"]] == [1, 2, 3, 4, 5, 6] and "| 6 | screenshot |" in (
        run.dir / "index.md").read_text(encoding="utf-8-sig")

    # Full page at 3x is clipped below Chromium's 16384 device-pixel limit.
    full = run.shot("long", full=True, viewport="phone")
    clip = [c for m, c in fake.calls if m == "Page.captureScreenshot"][-1]["clip"]
    assert full["truncated"] is True and clip["height"] * 3 <= h.MAX_CAPTURE_DEVICE_PX

    # A failing capture must not leave the slot stuck in phone/dark emulation.
    fake.fail_shot, fake.calls = True, []
    with pytest.raises(RuntimeError):
        run.matrix("broken", viewports=("phone",), schemes=("dark",))
    assert "Emulation.clearDeviceMetricsOverride" in _methods(fake)

    board = run.storyboard("Home teardown", critique={"status": "Spinner never resolves on phone"})
    text = open(board["html"], encoding="utf-8-sig").read()
    assert "src='001-home-phone-light.png'" in text and "Spinner never resolves" in text


@pytest.fixture
def managed_exec(monkeypatch):
    grants, runs = [], []

    def route_backend(env, session, task_id, local, **kw):
        return browser_control_route._apply_lease(env, grants[0], "http://control.invalid")

    def run(cmd, code, env, timeout):
        runs.append((code, dict(env)))
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(browser_control_route, "is_managed", lambda: True)
    monkeypatch.setattr(browser_use_cli, "_route_backend", route_backend)
    monkeypatch.setattr(browser_use_cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(browser_use_cli, "_run_cli_killing_process_group", run)
    monkeypatch.setattr(browser_use_cli, "_attach_vault_supervisor", lambda env, task_id: None)
    return grants, runs


def _lease(slot):
    return {"slot_id": slot, "browser_generation": "g1", "ownership_epoch": 1,
            "command_token": "tok-" + slot, "lease_id": "lease-" + slot}


def test_payment_slot_refuses_ux_capture_at_dispatch_and_in_the_harness(managed_exec, cap):
    grants, runs = managed_exec
    fake, ux = cap
    guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    code = "r = ux_capture.run('teardown')\nprint(r.audit_a11y())"

    grants[:] = [_lease("shopping")]
    refused = json.loads(browser_use_cli.browser_exec(code, task_id="t"))
    assert refused["error_type"] == "payment_screenshot_blocked" and runs == []

    grants[:] = [_lease("research-1")]
    assert json.loads(browser_use_cli.browser_exec(code, task_id="t"))["success"] is True
    sent, env = runs[0]
    assert sent.startswith("# hermes: ux capture helpers") and str(browser_capture.HARNESS_MODULE) in sent
    assert env[h.GUARD_ENV] == str(guard._state_path()) and env[h.ROOT_ENV].endswith("artifacts/ux-capture")
    # Code that never names the helpers is passed through unchanged.
    browser_use_cli.browser_exec("print(page_info())", task_id="t")
    assert "ux capture helpers" not in runs[1][0] and h.ROOT_ENV not in runs[1][1]

    # Second check inside the harness, from the same mark file: the research slot is clear,
    # the payment slot, a missing guard path and an unreadable file all refuse.
    ux.run("ok").shot("clear")
    for env_patch in ({h.SLOT_ENV: "shopping"}, {h.GUARD_ENV: ""}):
        blocked = h.UXCapture(fake.globals(), {**ux.env, **env_patch})
        with pytest.raises(h.CaptureRefused):
            blocked.run("x").shot("blocked")
    guard._state_path().write_text("{not json", encoding="utf-8")
    with pytest.raises(h.CaptureRefused):
        ux.run("x").matrix("blocked")


def test_axe_is_sha_pinned_and_sent_in_chunks_under_the_ipc_line_limit(cap, tmp_path):
    fake, ux = cap
    axe = tmp_path / "axe.min.js"
    axe.write_text("window.axe={run(){}};/*" + "é\"\\x" * 30000 + "*/", encoding="utf-8")
    ux.env[h.AXE_PATH_ENV] = str(axe)
    with pytest.raises(RuntimeError, match="pinned sha256"):
        ux.run("a").audit_a11y()
    ux.env[h.AXE_PATH_ENV + "_SHA256"] = hashlib.sha256(axe.read_bytes()).hexdigest()
    result = ux.run("a").audit_a11y()
    assert result["violations"] == 1 and result["by_impact"] == {"serious": 1}
    assert len(fake.isolated_sources) > 1
    assert all(len(json.dumps({"method": "Runtime.evaluate", "params": {"expression": s}})) < 65536
               for s in fake.isolated_sources)
    assert all(c.get("contextId") == 7 for m, c in fake.calls if m == "Runtime.evaluate")


def test_perf_rates_vitals_and_throttle_is_restored(cap):
    fake, ux = cap
    out = ux.run("p").perf(network="slow-4g", cpu=4, reload=True)
    assert out["ratings"]["lcp_ms"] == "needs-improvement" and out["ratings"]["cls"] == "good"
    net = [c for m, c in fake.calls if m == "Network.emulateNetworkConditions"]
    assert net[0]["latency"] == 150 and net[-1] == {"offline": False, "latency": 0,
                                                     "downloadThroughput": -1, "uploadThroughput": -1}
    assert [c["rate"] for m, c in fake.calls if m == "Emulation.setCPUThrottlingRate"] == [4.0, 1]
    with pytest.raises(ValueError):
        with ux.throttle(network="dial-up"):
            pass


def test_diff_and_baseline_store_pass_then_flag_change(cap):
    fake, ux = cap
    run = ux.run("reg")
    light = run.shot("home")
    assert run.baseline("home", light)["status"] == "stored"
    assert run.baseline("home", run.shot("home"))["status"] == "pass"
    fake.scheme = "dark"  # every pixel differs
    changed = run.baseline("home", run.shot("home"))
    assert changed["status"] == "changed" and changed["changed_ratio"] == 1.0
    assert open(changed["diff"], "rb").read().startswith(h.PNG_MAGIC)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_recording_acks_frames_keeps_other_events_and_writes_mp4(cap):
    fake, ux = cap
    run = ux.run("flow")
    rec = run.record("signup", gif=True, max_seconds=30)
    rec.start()
    for i in range(6):
        fake.events.append({"method": "Page.screencastFrame",
                            "params": {"data": _jpeg((40 * i, 80, 120)), "sessionId": i,
                                       "metadata": {"timestamp": 1000.0 + i * 0.2}}})
    fake.events.append({"method": "Network.loadingFinished", "params": {"requestId": "r1"}})
    # helpers.wait_for_network_idle drains through the patched function and still sees Network events.
    seen = ux.g["drain_events"]()
    out = rec.stop()
    assert [e["method"] for e in seen] == ["Network.loadingFinished"]
    assert sorted(c["sessionId"] for m, c in fake.calls if m == "Page.screencastFrameAck") == list(range(6))
    assert ux.g["drain_events"] == fake.drain_events  # restored
    mp4 = open(out["path"], "rb").read()
    assert mp4[4:8] == b"ftyp" and out["frames"] == 6 and out["gif"]["bytes"] > 0
    assert not list(run.dir.glob(".frames-*"))
