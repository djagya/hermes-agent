"""Managed control client: fair-queue wait, busy fields, key file, wipe counter env."""

import pytest

from tools import browser_control_route as route


@pytest.fixture(autouse=True)
def _managed(monkeypatch):
    route.clear_held_leases()
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://control.invalid")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "k")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(route, "_ambient_cdp_override", lambda: "")
    yield
    route.clear_held_leases()


def _lease(**extra):
    return {"lease_id": "l1", "slot_id": "research-1", "browser_generation": "g1", "ownership_epoch": 1,
            "command_token": "tok", **extra}


def _clock(monkeypatch):
    now = [0.0]
    slept = []
    monkeypatch.setattr(route, "_monotonic", lambda: now[0])

    def sleep(s):
        slept.append(s)
        now[0] += s

    monkeypatch.setattr(route, "_sleep", sleep)
    return slept


def test_queued_busy_waits_with_ticket_then_gets_the_slot(monkeypatch):
    slept = _clock(monkeypatch)
    replies = [{"error": "busy", "queued": "q-1", "ticket": "q-1", "position": 2, "retry_after_s": 4},
               {"error": "busy", "queued": "q-1", "ticket": "q-1", "position": 1, "retry_after_s": 4},
               _lease(wipe_count=3, profile_kind="temporary")]
    sent = []
    monkeypatch.setattr(route, "_post", lambda control, path, payload: (sent.append(payload), replies.pop(0))[1])
    env = {}
    assert route.resolve_managed_cdp(env, task_id="t", wait_s=30) is None
    assert slept == [4.0, 4.0]
    assert "ticket" not in sent[0] and sent[1]["ticket"] == "q-1" and sent[2]["ticket"] == "q-1"
    assert env[route.MANAGED_WIPE_ENV] == "3" and env[route.MANAGED_PROFILE_KIND_ENV] == "temporary"


def test_busy_past_budget_returns_ticket_fields(monkeypatch):
    _clock(monkeypatch)
    monkeypatch.setattr(route, "_post", lambda *a: {"error": "busy", "queued": "q-9", "position": 3,
                                                     "retry_after_s": 5, "detail": "waiting for a slot"})
    err = route.resolve_managed_cdp({}, task_id="t", wait_s=12)
    assert err.startswith("busy:") and "ticket q-9" in err
    assert err.fields == {"queued_id": "q-9", "position": 3, "retry_after_s": 5.0}


def test_human_held_busy_is_not_waited_on(monkeypatch):
    slept = _clock(monkeypatch)
    calls = []
    monkeypatch.setattr(route, "_post", lambda *a: (calls.append(1), {"error": "busy", "retry_after_s": 30,
                                                                       "detail": "research-1 is with a human"})[1])
    err = route.resolve_managed_cdp({}, task_id="t", wait_s=90)
    assert err.startswith("busy:") and calls == [1] and slept == []


def test_non_integer_wipe_count_leaves_env_unset(monkeypatch):
    monkeypatch.setattr(route, "_post", lambda *a: _lease(wipe_count="7"))
    env = {route.MANAGED_WIPE_ENV: "stale"}
    assert route.resolve_managed_cdp(env, task_id="t", wait_s=0) is None
    assert route.MANAGED_WIPE_ENV not in env


def test_control_key_file_wins_and_an_unreadable_file_means_no_key(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv(route.MANAGED_KEY_FILE_ENV, str(key))
    assert route.control_key() == "from-file"
    monkeypatch.setenv(route.MANAGED_KEY_FILE_ENV, str(tmp_path / "missing"))
    assert route.control_key() == ""
