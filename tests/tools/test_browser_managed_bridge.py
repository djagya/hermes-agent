"""Pinned managed-browser bridges reuse only the current task's live held lease."""

import base64
import io
import json
import os
import shutil
import subprocess
import zipfile
from types import SimpleNamespace
from typing import Any

import pytest

from tools import browser_control_route as route
from tools import browser_managed_bridge as bridge
from tools import browser_use_cli
from tools.registry import registry


_REAL_LOAD_BINDINGS = bridge._load_bindings
ORIGIN = "https://portal.example/"
LEASE_URL = "http://control.invalid/slot/research-1/json/version?tok=lease-secret"


def _binding(name="synthetic", **changes):
    value = {
        "origin": ORIGIN,
        "provider": "synthetic-provider",
        "identity": "sensitive",
        "mode": "research",
        "vault_handle": "op:synthetic-item",
        "selectors": {
            "username": "#email",
            "password": "#password",
            "form": "form",
            "submit": "button[type=submit]",
        },
        "ionic": False,
        "read_expressions": {
            "route": "route-expression",
            "capture": "capture-expression",
            "archive": "archive-expression",
        },
    }
    value.update(changes)
    return value


@pytest.fixture(autouse=True)
def _bindings(monkeypatch):
    monkeypatch.setattr(bridge, "_load_bindings", lambda: {"synthetic": _binding()})
    monkeypatch.setattr(route, "_ambient_cdp_override", lambda: "")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("BU_AUTOSPAWN", raising=False)
    route.clear_held_leases()
    yield
    route.clear_held_leases()


def _credential_owner(monkeypatch):
    backend = SimpleNamespace(
        get_meta=lambda handle: SimpleNamespace(
            kind="login",
            identifier="synthetic-user",
            allowed_origins=(ORIGIN,),
            origin=ORIGIN,
        ),
        resolve_password=lambda handle: "synthetic-password",
    )
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda handle: backend)
    return backend


def _transport(monkeypatch, targets=None):
    calls = []
    def held(env, **kwargs):
        env["BU_CDP_URL"] = LEASE_URL

    monkeypatch.setattr(bridge, "held_managed_cdp", held)
    monkeypatch.setattr(
        bridge,
        "_targets_from_lease",
        lambda url: list(targets if targets is not None else [
            {"id": "target-1", "type": "page", "url": ORIGIN},
        ]),
    )

    def evaluate(url, method, params, target_id, timeout):
        calls.append({"url": url, "method": method, "params": params, "timeout": timeout})
        expression = params.get("expression", "")
        if "return (route-expression);" in expression:
            value = {"ok": True, "detail_route": "/insurance/synthetic/detail"}
        elif "return (capture-expression);" in expression:
            value = {"ok": True, "inventory": [{"amount": "1.00"}], "total": "1.00"}
        elif "return (archive-expression);" in expression:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("synthetic.pdf", b"%PDF synthetic")
            value = {
                "ok": True,
                "archive_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                "document_count": 1,
            }
        else:
            value = {"ok": True, "form_valid": True}
        return {"result": {"value": value}}

    monkeypatch.setattr(bridge, "_cdp_call", evaluate)
    monkeypatch.setattr(bridge, "_run_async", lambda value: value)
    return calls


def test_held_lease_is_reused_without_acquire_or_process_env(monkeypatch):
    held = {
        "BU_CDP_URL": LEASE_URL,
        route.MANAGED_LEASE_ENV: "lease-1",
        route.MANAGED_TOKEN_ENV: "lease-secret",
    }
    monkeypatch.setattr(route, "_held_leases", {route._hold_key("task-1", "", "sensitive", "research"): held})
    monkeypatch.setattr(route, "control_url", lambda: "http://control.invalid")
    monkeypatch.setattr(route, "_lease_alive", lambda control, lease: True)
    monkeypatch.setattr(route, "_acquire_waiting", lambda **kwargs: pytest.fail("must not acquire"))

    env = {}
    assert route.held_managed_cdp(env, task_id="task-1", identity="sensitive", mode="research") is None
    assert env["BU_CDP_URL"] == LEASE_URL


@pytest.mark.parametrize("task_id,identity", [("other-task", "sensitive"), ("task-1", "other")])
def test_held_lease_refuses_task_and_identity_mismatch(monkeypatch, task_id, identity):
    key = route._hold_key("task-1", "", "sensitive", "research")
    monkeypatch.setattr(route, "_held_leases", {key: {"BU_CDP_URL": LEASE_URL, route.MANAGED_LEASE_ENV: "lease-1"}})
    monkeypatch.setattr(route, "control_url", lambda: "http://control.invalid")
    monkeypatch.setattr(route, "_acquire_waiting", lambda **kwargs: pytest.fail("must not acquire"))
    assert route.held_managed_cdp({}, task_id=task_id, identity=identity, mode="research") == "lease_not_held"


@pytest.mark.parametrize("alive,error", [(False, "lease_stale"), (None, "lease_unavailable")])
def test_held_lease_refuses_stale_or_unknown_state(monkeypatch, alive, error):
    key = route._hold_key("task-1", "", "sensitive", "research")
    held = {"BU_CDP_URL": LEASE_URL, route.MANAGED_LEASE_ENV: "lease-1"}
    monkeypatch.setattr(route, "_held_leases", {key: held})
    monkeypatch.setattr(route, "control_url", lambda: "http://control.invalid")
    monkeypatch.setattr(route, "_lease_alive", lambda control, lease: alive)
    monkeypatch.setattr(route, "_acquire_waiting", lambda **kwargs: pytest.fail("must not acquire"))
    assert route.held_managed_cdp({}, task_id="task-1", identity="sensitive", mode="research") == error


def test_held_lease_refuses_handoff_until_handback(monkeypatch):
    key = route._hold_key("task-1", "", "sensitive", "research")
    held = {
        "BU_CDP_URL": LEASE_URL,
        route.MANAGED_SLOT_ENV: "slot-1",
        route.MANAGED_LEASE_ENV: "lease-1",
    }
    monkeypatch.setattr(route, "_held_leases", {key: held})
    monkeypatch.setattr(route, "_minted_handoffs", {})
    monkeypatch.setattr(route, "control_url", lambda: "http://control.invalid")
    monkeypatch.setattr(route, "_lease_alive", lambda control, lease: True)
    route.note_handoff("task-1", "slot-1", "lease-1", 60)
    assert route.held_managed_cdp({}, task_id="task-1", identity="sensitive") == "lease_handed_off"
    assert route.claim_handback("task-1", "slot-1", "lease-1") is True
    assert route.held_managed_cdp({}, task_id="task-1", identity="sensitive") is None


def test_held_lease_refuses_ambient_cdp_override(monkeypatch):
    monkeypatch.setattr(route, "control_url", lambda: "http://control.invalid")
    monkeypatch.setattr(route, "_ambient_cdp_override", lambda: "ws://attacker.invalid/devtools")
    monkeypatch.setattr(route, "_acquire_waiting", lambda **kwargs: pytest.fail("must not acquire"))
    error = route.held_managed_cdp(
        {}, task_id="task-1", identity="sensitive", mode="research",
    )
    assert error is not None and error.startswith("scope_denied:")


def test_fill_uses_vault_owner_ionic_events_and_never_submits(monkeypatch):
    _credential_owner(monkeypatch)
    calls = _transport(monkeypatch)
    monkeypatch.setattr(bridge, "_load_bindings", lambda: {"synthetic": _binding(ionic=True)})

    result = bridge.run_managed_bridge("synthetic", "fill", task_id="task-1")
    assert result == {"success": True, "credentials_populated": True}
    expression = calls[0]["params"]["expression"]
    assert "ionInput" in expression and "ionChange" in expression and "ng-valid" in expression
    assert "synthetic-password" in expression  # remains inside the protected owner only
    assert ".click(" not in expression and ".submit(" not in expression
    assert LEASE_URL not in json.dumps(result)


def test_challenge_and_unknown_binding_fail_closed_without_secret_leakage(monkeypatch):
    _credential_owner(monkeypatch)
    _transport(monkeypatch)
    monkeypatch.setattr(bridge, "_evaluate_expression", lambda *args, **kwargs: {"ok": False, "error": "challenge_refused"})
    refused = bridge.run_managed_bridge("synthetic", "fill", task_id="task-1")
    unknown = bridge.run_managed_bridge("unknown", "fill", task_id="task-1")
    assert refused["error"] == "challenge_refused"
    assert unknown["error"] == "unknown_binding"
    for result in (refused, unknown):
        text = json.dumps(result)
        assert "synthetic-password" not in text and "lease-secret" not in text


@pytest.mark.parametrize("action", ["route", "capture", "archive"])
def test_read_actions_use_only_pinned_expressions(monkeypatch, action, tmp_path):
    calls = _transport(monkeypatch)
    result = bridge.run_managed_bridge(f"synthetic-read-{action}", action, task_id="task-1", bindings={
        f"synthetic-read-{action}": _binding(
            archive_root=str(tmp_path), archive_name="synthetic-documents",
        ),
    })
    assert result["success"] is True, result
    expression = calls[0]["params"]["expression"]
    assert f"return ({action}-expression);" in expression
    assert expression.index("window.location.origin") < expression.index(f"{action}-expression")
    assert expression.index("challenge_refused") < expression.index(f"{action}-expression")
    assert calls[0]["params"]["awaitPromise"] is True
    assert "archive_base64" not in result
    assert "lease-secret" not in json.dumps(result)


@pytest.mark.parametrize("index", range(7))
def test_all_seven_login_binding_shapes_fill_without_submit(monkeypatch, index):
    _credential_owner(monkeypatch)
    calls = _transport(monkeypatch)
    name = f"provider-{index}"
    selectors = _binding()["selectors"] if index == 0 else {"username": f"#user-{index}", "password": f"#pass-{index}"}
    monkeypatch.setattr(bridge, "_load_bindings", lambda: {name: _binding(selectors=selectors, ionic=index == 0)})
    result = bridge.run_managed_bridge(name, "fill", task_id="task-1")
    assert result["success"] is True
    expression = calls[0]["params"]["expression"]
    assert selectors["username"] in expression and selectors["password"] in expression
    assert ".click(" not in expression and ".submit(" not in expression


@pytest.mark.parametrize("targets,error", [
    ([], "target_not_found"),
    ([{"id": "a", "type": "page", "url": "https://wrong.example/"}], "target_not_found"),
    ([{"id": "a", "type": "page", "url": ORIGIN}, {"id": "b", "type": "page", "url": ORIGIN}], "target_ambiguous"),
])
def test_wrong_missing_and_ambiguous_origins_fail_closed(monkeypatch, targets, error):
    _credential_owner(monkeypatch)
    _transport(monkeypatch, targets=targets)
    assert bridge.run_managed_bridge("synthetic", "fill", task_id="task-1")["error"] == error


def test_browser_exec_dispatches_pinned_bridge_and_refuses_overrides(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "run_managed_bridge", lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True})
    raw = registry.dispatch("browser_exec", {"bridge": "synthetic", "action": "route"}, task_id="task-1")
    assert json.loads(raw)["success"] is True
    assert calls[0][0] == ("synthetic", "route")

    refused = json.loads(registry.dispatch("browser_exec", {
        "bridge": "synthetic", "action": "route", "code": "print('arbitrary')",
    }, task_id="task-1"))
    assert refused["error"] == "bridge_override_refused"
    assert len(calls) == 1


def test_managed_schema_exposes_bridge_without_making_code_required(monkeypatch):
    monkeypatch.setattr(route, "is_managed", lambda: True)
    parameters = browser_use_cli._dynamic_schema_overrides()["parameters"]
    assert parameters["required"] == []
    assert parameters["properties"]["action"]["enum"] == ["verify", "fill", "route", "capture", "archive"]
    assert "bridge" in parameters["properties"]


def test_archive_never_overwrites_and_sensitive_read_fields_fail_closed(monkeypatch, tmp_path):
    binding = _binding(archive_root=str(tmp_path), archive_name="synthetic-documents")
    calls = _transport(monkeypatch)
    assert bridge.run_managed_bridge("synthetic", "archive", task_id="task-1", bindings={
        "synthetic": binding,
    })["success"] is True
    before = list(tmp_path.rglob("*.zip"))
    assert len(before) == 1
    original = before[0].read_bytes()
    duplicate = bridge.run_managed_bridge("synthetic", "archive", task_id="task-1", bindings={
        "synthetic": binding,
    })
    assert duplicate["error"] == "archive_exists"
    assert before[0].read_bytes() == original

    def sensitive(url, method, params, target_id, timeout):
        return {"result": {"value": {"ok": True, "access_token": "must-not-escape"}}}

    monkeypatch.setattr(bridge, "_cdp_call", sensitive)
    refused = bridge.run_managed_bridge("synthetic", "route", task_id="task-1")
    assert refused == {"success": False, "error": "response_refused"}
    assert calls


@pytest.mark.parametrize("action", ["verify", "fill"])
@pytest.mark.parametrize("task_id", [None, "unowned-task"])
def test_no_credential_resolution_before_owned_lease(monkeypatch, action, task_id):
    monkeypatch.setattr(bridge, "held_managed_cdp", lambda *args, **kwargs: "lease_not_held")
    monkeypatch.setattr(bridge, "_resolve_credentials", lambda *args: pytest.fail("unowned vault access"))
    result = bridge.run_managed_bridge("synthetic", action, task_id=task_id)
    assert result == {"success": False, "error": "lease_not_held"}


def test_login_expression_checks_current_origin_before_fields():
    expression = bridge._login_expression(
        {"username": "synthetic-user", "password": "synthetic-password"}, _binding(),
    )
    assert 'window.location.origin !== "https://portal.example"' in expression
    assert expression.index("origin_mismatch") < expression.index("const user=")


def test_stale_lease_retires_its_harness_daemon(monkeypatch):
    key = route._hold_key("task-1", "", "sensitive", "research")
    held = {"BU_CDP_URL": LEASE_URL, route.MANAGED_LEASE_ENV: "lease-1", "BU_NAME": "bu-ctrl-slot-1-g-1"}
    retired = []
    monkeypatch.setattr(route, "_held_leases", {key: held})
    monkeypatch.setattr(route, "control_url", lambda: "http://control.invalid")
    monkeypatch.setattr(route, "_lease_alive", lambda control, lease: False)
    monkeypatch.setattr(route, "_retire_daemon", retired.append)
    assert route.held_managed_cdp({}, task_id="task-1", identity="sensitive") == "lease_stale"
    assert retired == ["bu-ctrl-slot-1-g-1"]
    assert key not in route._held_leases


def test_browser_exec_without_code_or_bridge_keeps_the_usage_error():
    raw = registry.dispatch("browser_exec", {}, task_id="task-1")
    assert "No code provided" in raw


@pytest.mark.parametrize("case", [
    "ok", "writable", "other_owner", "symlink", "parent_symlink", "hardlink", "root_gateway", "not_object",
])
def test_bindings_load_only_from_an_operator_owned_file(monkeypatch, tmp_path, case):
    home = tmp_path / "home"
    real_dir = tmp_path / "elsewhere" if case == "parent_symlink" else home / "browser"
    real_dir.mkdir(parents=True)
    source = real_dir / ("other.json" if case in {"symlink", "hardlink"} else "managed-bridges.json")
    source.write_text(json.dumps([] if case == "not_object" else {"synthetic": _binding()}))
    source.chmod(0o666 if case == "writable" else 0o644)
    path = home / "browser" / "managed-bridges.json"
    if case == "parent_symlink":
        home.mkdir()
        (home / "browser").symlink_to(real_dir)
    elif case == "symlink":
        path.symlink_to(source)
    elif case == "hardlink":
        os.link(source, path)
    monkeypatch.setattr(bridge, "_bindings_path", lambda: path)
    monkeypatch.setattr(bridge, "_OPERATOR_UID", os.getuid() + (1 if case == "other_owner" else 0))
    monkeypatch.setattr(bridge, "_gateway_is_root", lambda: case == "root_gateway")
    assert _REAL_LOAD_BINDINGS() == ({"synthetic": _binding()} if case == "ok" else {})


def test_bindings_path_is_fixed_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert bridge._bindings_path() == tmp_path / "browser" / "managed-bridges.json"


_NODE = shutil.which("node")


def _run_js(expression, *, origin, challenge=False, ionic=False):
    """Execute a generated expression against a fake DOM in Node; report writes, events, clicks."""
    harness = """
const state={writes:{},events:[],clicked:false,readStarted:false};
const field=(name)=>({set value(v){state.writes[name]=v;},get value(){return state.writes[name]||'';},
  dispatchEvent(e){state.events.push(e.type);}});
const nodes={'#email':field('user'),'#password':field('pass'),
  'form':{classList:{contains:(c)=>c==='ng-valid'}},'button[type=submit]':{click(){state.clicked=true;}}};
globalThis.window={location:{origin:ORIGIN}};
globalThis.document={querySelector:(s)=>s.includes('captcha')?(CHALLENGE?{}:null):(nodes[s]||null)};
globalThis.Event=class{constructor(t){this.type=t;}};
globalThis.CustomEvent=globalThis.Event;
globalThis.state=state;
const result=eval(EXPRESSION);
process.stdout.write(JSON.stringify({result,...state}));
""".replace("ORIGIN", json.dumps(origin)).replace("CHALLENGE", json.dumps(challenge)).replace(
        "EXPRESSION", json.dumps(expression))
    assert _NODE is not None
    done = subprocess.run([_NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("ionic", [False, True])
@pytest.mark.parametrize("origin,challenge,expected", [
    ("https://wrong.example", False, "origin_mismatch"),
    ("https://portal.example", True, "challenge_refused"),
    ("https://portal.example", False, None),
])
def test_fill_expression_checks_page_at_use_and_never_submits(origin, challenge, expected, ionic):
    expression = bridge._login_expression(
        {"username": "synthetic-user", "password": "synthetic-password"}, _binding(ionic=ionic),
    )
    observed = _run_js(expression, origin=origin, challenge=challenge)
    assert observed["clicked"] is False
    if expected:
        assert observed["result"] == {"ok": False, "error": expected}
        assert observed["writes"] == {}
    else:
        assert observed["result"]["ok"] is True
        assert observed["writes"] == {"user": "synthetic-user", "pass": "synthetic-password"}
        per_field = ["ionInput", "ionChange", "ionBlur"] if ionic else ["input", "change"]
        assert observed["events"] == per_field * 2


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("origin,challenge,expected", [
    ("https://wrong.example", False, "origin_mismatch"),
    ("https://portal.example", True, "challenge_refused"),
    ("https://portal.example", False, None),
])
def test_read_guard_runs_before_the_pinned_body(origin, challenge, expected):
    expression = bridge._guard_read_expression("(() => { state.readStarted = true; return {ok: true}; })()",
                                               _binding())
    observed = _run_js(expression, origin=origin, challenge=challenge)
    assert observed["readStarted"] is (expected is None)
    assert observed["result"].get("error") == expected


def test_vault_item_for_another_origin_is_never_resolved(monkeypatch):
    backend = _credential_owner(monkeypatch)
    backend.get_meta = lambda handle: SimpleNamespace(kind="login", identifier="synthetic-user",
                                                      allowed_origins=("https://other.example",), origin=None)
    backend.resolve_password = lambda handle: pytest.fail("password resolved for a foreign origin")
    _transport(monkeypatch)
    assert bridge.run_managed_bridge("synthetic", "fill", task_id="task-1")["error"] == "credential_resolution_failed"


def test_missing_task_refuses_before_any_lease_lookup(monkeypatch):
    monkeypatch.setattr(bridge, "held_managed_cdp", lambda *a, **k: pytest.fail("lease lookup without a task"))
    monkeypatch.setattr(bridge, "_resolve_credentials", lambda *a: pytest.fail("vault access without a task"))
    assert bridge.run_managed_bridge("synthetic", "fill", task_id=None) == {"success": False, "error": "lease_not_held"}


def test_handoff_minted_during_vault_call_refuses_the_fill(monkeypatch):
    _credential_owner(monkeypatch)
    calls = _transport(monkeypatch)
    answers = iter([None, "lease_handed_off"])

    def held(env, **kwargs):
        env["BU_CDP_URL"] = LEASE_URL
        return next(answers)

    monkeypatch.setattr(bridge, "held_managed_cdp", held)
    assert bridge.run_managed_bridge("synthetic", "fill", task_id="task-1")["error"] == "lease_handed_off"
    assert calls == []


@pytest.mark.parametrize("override", [
    {"code": "print(1)"}, {"session": "s"}, {"local": True}, {"mode": "qa"}, {"identity": "other"},
    {"qa_targets": ["https://x.example"]}, {"on_dialog": "accept"}, {"timeout_s": 5},
])
def test_bridge_refuses_every_route_override(override):
    args: dict[str, Any] = dict(binding_id="synthetic", action="route", code="", session="", timeout_s=60,
                                default_timeout_s=60, local=False, mode="", identity="", qa_targets=None,
                                on_dialog="auto", task_id="task-1")
    args.update(override)
    assert bridge.dispatch_browser_exec(**args) == {"error": "bridge_override_refused"}


@pytest.mark.parametrize("key", ["accessToken", "refresh-token", "Set-Cookie", "sessionId", "apiKey"])
def test_read_refuses_secret_keys_in_any_spelling(monkeypatch, key):
    _transport(monkeypatch)
    monkeypatch.setattr(bridge, "_cdp_call", lambda *a: {"result": {"value": {"ok": True, "nested": [{key: "x"}]}}})
    assert bridge.run_managed_bridge("synthetic", "route", task_id="task-1")["error"] == "response_refused"


def test_transport_exception_never_reaches_the_caller(monkeypatch):
    _transport(monkeypatch)

    def broken(*args):
        raise RuntimeError(f"CDP error at {LEASE_URL}")

    monkeypatch.setattr(bridge, "_cdp_call", broken)
    monkeypatch.setattr(bridge, "_evaluate_expression", lambda *a, **k: broken())
    assert bridge.run_managed_bridge("synthetic", "route", task_id="task-1") == {"success": False, "error": "bridge_failed"}


def _zip(names):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"%PDF synthetic")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.mark.parametrize("value,error", [
    ({"ok": True, "archive_base64": _zip(["a.pdf", "b.pdf"]), "document_count": 1}, "archive_validation_failed"),
    ({"ok": True, "archive_base64": base64.b64encode(b"not a zip").decode(), "document_count": 0},
     "archive_validation_failed"),
    ({"ok": True, "archive_base64": "%%%", "document_count": 1}, "archive_validation_failed"),
    ({"ok": True, "archive_base64": _zip(["a.pdf"])}, "archive_validation_failed"),
])
def test_archive_rejects_bad_exports_and_writes_nothing(monkeypatch, tmp_path, value, error):
    _transport(monkeypatch)
    monkeypatch.setattr(bridge, "_cdp_call", lambda *a: {"result": {"value": value}})
    binding = _binding(archive_root=str(tmp_path), archive_name="synthetic-documents")
    assert bridge.run_managed_bridge("s", "archive", task_id="task-1", bindings={"s": binding})["error"] == error
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_archive_refuses_a_symlinked_date_directory(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    date = bridge.datetime.now(bridge.ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d")
    (root / date).symlink_to(outside)
    binding = _binding(archive_root=str(root), archive_name="synthetic-documents")
    result = bridge.run_managed_bridge("s", "archive", task_id="task-1", bindings={"s": binding})
    assert result["error"] == "archive_validation_failed" and calls
    assert list(outside.rglob("*.zip")) == []


def test_existing_archive_refuses_before_the_page_export_runs(monkeypatch, tmp_path):
    calls = _transport(monkeypatch)
    binding = _binding(archive_root=str(tmp_path), archive_name="synthetic-documents")
    bridge._archive_destination(binding).parent.mkdir(parents=True)
    bridge._archive_destination(binding).write_bytes(b"PK")
    assert bridge.run_managed_bridge("s", "archive", task_id="task-1", bindings={"s": binding})["error"] == "archive_exists"
    assert calls == []
