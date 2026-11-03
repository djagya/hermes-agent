"""Pinned managed-browser bridges reuse only the current task's live held lease."""

import base64
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from tools import browser_control_route as route
from tools import browser_managed_bridge as bridge
from tools import browser_use_cli
from tools.registry import registry


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
        if expression == "route-expression":
            value = {"ok": True, "detail_route": "/insurance/synthetic/detail"}
        elif expression == "capture-expression":
            value = {"ok": True, "inventory": [{"amount": "1.00"}], "total": "1.00"}
        elif expression == "archive-expression":
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
    assert calls[0]["params"]["expression"] == f"{action}-expression"
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
