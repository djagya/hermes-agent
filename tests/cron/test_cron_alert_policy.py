"""Per-job alert policy: alert on N failures in a row or once failing for M minutes, not on the
first blip; keep the 6 h reminder; name the failing step; say when the job recovered.

Field report (2026-10): read-only */10 refresh jobs pinged Telegram 12-14 times a day for single
transient failures whose last good snapshot was kept anyway, so real outages drowned.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron import incidents
import cron.jobs as cron_jobs
import cron.scheduler as sched
from hermes_time import now as _hermes_now


def _job(**overrides):
    job = {
        "id": "alert-policy-test", "name": "alert policy test", "prompt": "hello",
        "enabled": True, "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 10, "display": "every 10m"},
        "deliver": "telegram:123", "model": None, "provider": None,
        "provider_snapshot": "openrouter", "base_url": None,
    }
    job.update(overrides)
    return job


def _tick(job_id, tmp_path, deliveries, *, error=None):
    """One real run_one_job tick; the agent raises ``error`` or answers "ok"."""
    def fake_deliver(jb, content, adapters=None, loop=None, **kwargs):
        deliveries.append((content, kwargs.get("for_failure", False)))

    job = next(j for j in cron_jobs.load_jobs() if j["id"] == job_id)
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
             "api_key": "k", "base_url": "https://example.invalid/v1",
             "provider": "openrouter", "api_mode": "chat_completions"}), \
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as agent_cls:
        agent = MagicMock()
        if error:
            agent.run_conversation.side_effect = RuntimeError(error)
        else:
            agent.run_conversation.return_value = {"final_response": "ok"}
        agent_cls.return_value = agent
        sched.run_one_job(dict(job))


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    (tmp_path / "config.yaml").write_text("cron:\n  preflight: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with cron_jobs.use_cron_store(tmp_path):
        yield tmp_path


def test_streak_threshold_alerts_once_reached_then_reports_recovery(store):
    deliveries = []
    cron_jobs.save_jobs([_job(alert_after_consecutive_failures=3)])
    for _ in range(2):
        _tick("alert-policy-test", store, deliveries, error="mail provider 503")
    assert deliveries == [], "failures below the threshold are recorded, not pinged"
    assert incidents.count_incidents() == 1

    _tick("alert-policy-test", store, deliveries, error="mail provider 503")
    assert len(deliveries) == 1 and deliveries[0][1] is True
    assert "Failed 3 runs in a row" in deliveries[0][0]
    _tick("alert-policy-test", store, deliveries, error="mail provider 503")
    assert len(deliveries) == 1, "the 6 h reminder cooldown still applies"

    _tick("alert-policy-test", store, deliveries)
    recovery, output = deliveries[-2:]
    assert recovery[1] is True, "recovery goes to the failure lane"
    assert "recovered after 4 failed runs" in recovery[0]
    assert output == ("ok", False), "the run's own output is still delivered"
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert stored["failure_streak"] == 0 and "failing_since" not in stored

    # A single blip after recovery stays quiet again, and a green run after an unalerted
    # failure sends no recovery message.
    count = len(deliveries)
    _tick("alert-policy-test", store, deliveries, error="mail provider 503")
    _tick("alert-policy-test", store, deliveries)
    assert len(deliveries) == count + 1 and "ok" == deliveries[-1][0]


def test_without_policy_first_failure_alerts_and_no_recovery_message(store):
    deliveries = []
    cron_jobs.save_jobs([_job()])
    _tick("alert-policy-test", store, deliveries, error="boom")
    assert len(deliveries) == 1
    _tick("alert-policy-test", store, deliveries)
    assert [c for c, _ in deliveries[1:]] == ["ok"]


def test_stale_minutes_policy_and_unknown_start_alerts():
    now = _hermes_now()
    job = _job(alert_when_stale_minutes=30, failure_streak=0)
    assert sched._alert_withheld_by_policy(job), "first failure: failing for 0 min"
    job.update(failure_streak=2, failing_since=(now - timedelta(minutes=20)).isoformat())
    assert sched._alert_withheld_by_policy(job)
    job["failing_since"] = (now - timedelta(minutes=31)).isoformat()
    assert not sched._alert_withheld_by_policy(job)
    job.pop("failing_since")
    assert not sched._alert_withheld_by_policy(job), "a streak with no start stamp alerts"
    # Both set: whichever comes first.
    job.update(alert_after_consecutive_failures=3, failing_since=now.isoformat())
    assert not sched._alert_withheld_by_policy(job)


def test_alert_names_the_failing_step_from_long_script_stderr():
    stderr = "\n".join([f"step {i}: fetched" for i in range(40)]
                       + ["home: error, cause=connection-refused", "done"])
    error = f"Script exited with code 1\nstderr:\n{stderr}\nstdout:\nsummary written"
    job = _job(no_agent=True, alert_after_consecutive_failures=3, failure_streak=2)
    notice = sched._summarize_cron_failure_for_delivery(job, error)
    tail = sched._failure_alert_tail(job, error, notice)
    assert "Error line: home: error, cause=connection-refused" in tail


@pytest.mark.parametrize("value,expected", [(3, 3), ("5", 5), (0, None), ("", None), (None, None)])
def test_threshold_normalizer(value, expected):
    assert cron_jobs._normalize_alert_threshold(value) == expected


@pytest.mark.parametrize("value", [-1, 2.5, "x"])
def test_threshold_normalizer_rejects(value):
    with pytest.raises(ValueError):
        cron_jobs._normalize_alert_threshold(value)


def test_cli_edit_sets_and_clears_policy(store):
    from tools.cronjob_tools import cronjob
    import json

    cron_jobs.save_jobs([_job()])
    out = json.loads(cronjob(action="update", job_id="alert-policy-test",
                             alert_after_consecutive_failures=3, alert_when_stale_minutes=30))
    assert out["success"], out
    assert out["job"]["alert_after_consecutive_failures"] == 3
    out = json.loads(cronjob(action="update", job_id="alert-policy-test",
                             alert_after_consecutive_failures=0))
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert stored.get("alert_after_consecutive_failures") is None
    assert stored["alert_when_stale_minutes"] == 30
