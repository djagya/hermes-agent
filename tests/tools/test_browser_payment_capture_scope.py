"""Post-card-fill capture guard scoped to the managed slot control granted.

Synthetic leases only: no Chrome, no card data. The vault fill path itself is
covered by test_browser_vault_payment_live.py / _split.py.
"""

import json
import os
import subprocess
import sys

import pytest

from tools import browser_control_route, browser_payment_privacy as guard, browser_use_cli

PNG = "\x89PNG\r\n\x1a\n-synthetic"


def _lease(slot, gen="g1"):
    return {"slot_id": slot, "browser_generation": gen, "ownership_epoch": 1,
            "command_token": "tok-" + slot, "lease_id": "lease-" + slot}


@pytest.fixture
def managed(monkeypatch):
    """browser_exec on a fake control that grants the queued slots in order."""
    grants, runs = [], []

    def route_backend(env, session, task_id, local, **kw):
        lease = grants.pop(0) if len(grants) > 1 else grants[0]
        return browser_control_route._apply_lease(env, lease, "http://control.invalid")

    def run(cmd, code, env, timeout):
        runs.append(env.get("HERMES_BROWSER_SLOT_ID"))
        return subprocess.CompletedProcess(cmd, 0, PNG if "screenshot" in code else "Example Domain\n", "")

    monkeypatch.setattr(browser_control_route, "is_managed", lambda: True)
    monkeypatch.setattr(browser_use_cli, "_route_backend", route_backend)
    monkeypatch.setattr(browser_use_cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(browser_use_cli, "_run_cli_killing_process_group", run)
    monkeypatch.setattr(browser_use_cli, "_attach_vault_supervisor", lambda env, task_id: None)
    yield grants, runs


def _exec(code, task="t"):
    return json.loads(browser_use_cli.browser_exec(code, task_id=task))


def test_fill_blocks_its_slot_and_leaves_an_independent_research_slot_capturable(managed):
    grants, runs = managed
    guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})

    grants[:] = [_lease("research-1")]
    ok = _exec("capture_screenshot('page.png')")
    assert ok["success"] is True and ok["output"].startswith("\x89PNG")

    grants[:] = [_lease("shopping")]
    assert _exec("capture_screenshot('page.png')")["error_type"] == "payment_screenshot_blocked"
    # Text inspection of the payment slot is not a capture.
    assert _exec("print(page_info())")["success"] is True
    assert runs == ["research-1", "shopping"]


def test_aliases_and_generation_changes_keep_the_slot_blocked(managed):
    grants, _ = managed
    guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    for task, gen in (("other-task", "g1"), ("renamed-session", "g2"), ("checkout", "g9")):
        grants[:] = [_lease("shopping", gen)]
        assert _exec("cdp('Page.printToPDF')", task)["error_type"] == "payment_screenshot_blocked"


def test_reroute_to_the_payment_slot_is_rechecked(managed, monkeypatch):
    grants, runs = managed
    guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    grants[:] = [_lease("research-1"), _lease("shopping")]
    real_run = browser_use_cli._run_cli_killing_process_group

    def first_call_blocked(cmd, code, env, timeout):
        if not runs:
            runs.append(env.get("HERMES_BROWSER_SLOT_ID"))
            return subprocess.CompletedProcess(cmd, 1, "", "permission-blocked")
        return real_run(cmd, code, env, timeout)

    monkeypatch.setattr(browser_use_cli, "_run_cli_killing_process_group", first_call_blocked)
    assert _exec("capture_screenshot('x.png')")["error_type"] == "payment_screenshot_blocked"
    assert runs == ["research-1"]


def test_unknown_surface_profile_marks_and_unreadable_state_fail_closed(managed):
    grants, _ = managed
    assert guard.capture_blocked_for_surface("", "") is False  # no marks at all
    guard.mark_payment_session("unmanaged-fill")  # no slot identity: whole profile
    assert guard.capture_blocked_for_surface("research-1", "g1") is True
    grants[:] = [_lease("research-1")]
    assert _exec("capture_screenshot('x.png')")["error_type"] == "payment_screenshot_blocked"

    assert guard.clear_payment_surface("profile", confirmed_by="test") == 1
    guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": ""})
    assert guard.capture_blocked_for_surface("research-1", "g1") is True  # partial identity = profile

    guard.clear_payment_surface("profile", confirmed_by="test")
    guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    assert guard.capture_blocked_for_surface("", "g1") is True
    guard._state_path().write_text("{not json", encoding="utf-8")
    assert guard.capture_blocked_for_surface("research-1", "g1") is True
    assert guard.payment_session_sensitive(None) is True
    with pytest.raises(OSError):
        guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})


def test_tools_without_a_slot_identity_stay_profile_wide():
    guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    # browser_cdp, browser_vision, built-in screenshot/record/pdf and desktop capture
    # call this; it must not narrow to the slot.
    assert guard.payment_session_sensitive("an-unrelated-research-task") is True
    from tools.computer_use.tool import handle_computer_use

    assert json.loads(handle_computer_use({"action": "capture"}))["error_type"] == "payment_screenshot_blocked"


def test_marks_survive_a_gateway_restart_and_clear_only_by_operator(tmp_path):
    guard.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    probe = ("from tools.browser_payment_privacy import capture_blocked_for_surface as b;"
             "print(b('shopping', 'g2'), b('research-1', 'g1'))")
    fresh = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                           env=dict(os.environ), cwd=os.getcwd(), check=True)
    assert fresh.stdout.split() == ["True", "False"]
    assert oct(guard._state_path().stat().st_mode & 0o777) == "0o600"

    with pytest.raises(ValueError):
        guard.clear_payment_surface("shopping", confirmed_by="")
    assert guard.main(["clear", "shopping", "--confirmed-by", "operator: tab closed"]) == 0
    assert guard.capture_blocked_for_surface("shopping", "g2") is False
    audit = [json.loads(line) for line in guard._state_path().with_suffix(".log").read_text().splitlines()]
    assert audit[-1]["slot_id"] == "shopping" and audit[-1]["confirmed_by"] == "operator: tab closed"
