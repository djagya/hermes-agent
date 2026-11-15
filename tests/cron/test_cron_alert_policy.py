"""Per-job alert policy: alert on N failures in a row or once failing for M minutes, not on the
first blip; keep the 6 h reminder; name the failing step; no recovery ping.

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


def _tick(job_id, tmp_path, deliveries, *, error=None, answer="ok"):
    """One real run_one_job tick; the agent raises ``error`` or answers ``answer``."""
    def fake_deliver(jb, content, adapters=None, loop=None, **kwargs):
        deliveries.append((content, kwargs.get("for_failure", False)))

    job = next(j for j in cron_jobs.load_jobs() if j["id"] == job_id)
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
             "api_key": "k", "base_url": "https://example.invalid/v1",
             "provider": "openrouter", "api_mode": "chat_completions"}), \
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as agent_cls:
        agent = MagicMock()
        if error:
            agent.session_api_calls = 0  # never reached the model (cron.unreachable_retry)
            agent.run_conversation.side_effect = (
                error if isinstance(error, BaseException) else RuntimeError(error))
        else:
            agent.run_conversation.return_value = {"final_response": answer}
        agent_cls.return_value = agent
        sched.run_one_job(dict(job))
        # dlz: since v0.23.0 a run with an execution id queues its result and failure notice
        # (cron/delivery_queue.py); the gateway housekeeping tick drains it through
        # _deliver_result. Drain here like that tick, so the notice reaches the patched sender.
        sched.drain_delivery_queue(adapters={}, loop=None)


@pytest.fixture
def store(monkeypatch, tmp_path, cron_owner):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    (tmp_path / "config.yaml").write_text("cron:\n  preflight: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with cron_jobs.use_cron_store(tmp_path):
        yield tmp_path


def test_streak_threshold_alerts_once_reached_then_recovers_silently(store):
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
    assert len(deliveries) == 2, "no recovery notice: only the run's own output follows"
    assert deliveries[-1] == ("ok", False), "the run's own output is still delivered"
    assert incidents.count_alerted_incidents("alert-policy-test") == 0, "the incident is still closed"
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


# Network-class failures (cron/transient_network.py): recorded, never counted, never pinged.
_DNS_ERRORS = [
    "curl: (6) Could not resolve host: api.example.invalid",
    "Script exited with code 1\nstderr:\nssh: Temporary failure in name resolution\nstdout:\n",
    "ConnectError: [Errno -2] Name or service not known",
    "httpx.ConnectError: all connection attempts failed",
    "ConnectionResetError: [Errno 104] Connection reset by peer",
    "OSError: [Errno 111] Connection refused",
    "net/http: TLS handshake timeout",
]


@pytest.mark.parametrize("text", _DNS_ERRORS)
def test_transient_network_signatures_match(text):
    from cron.transient_network import is_transient_network_failure

    assert is_transient_network_failure(text)


@pytest.mark.parametrize("text", [
    "", "mail provider 503", "RuntimeError: boom", "401 Unauthorized", "Script timed out after 60s: x.sh",
    "KeyError: 'connection'",
    # Credential, git and bundler messages that merely contain "failed to resolve".
    "RuntimeError: Failed to resolve a Nous inference API key",
    "fatal: Failed to resolve 'HEAD' as a valid ref",
    'error during build: Rollup failed to resolve import "x" from "y"',
    # An exception name that is not in exception-prefix position.
    "ImportError: cannot import name ConnectError from adapter",
    "RuntimeError: ImportError: cannot import name ConnectError",
    # A script's stdout is its own report, never the reason the run failed.
    "Script exited with code 1\nstderr:\nKeyError: 'x'\nstdout:\nconnection reset by peer\nConnectError: x",
    "Script exited with code 1\nstdout:\nCould not resolve host: a.invalid",
    # Long stdout ending in a network phrase cannot push the real stderr error out of view.
    "Script exited with code 1\nstderr:\nKeyError: 'x'\nstdout:\n" + "report line\n" * 1000 + "connection reset by peer",
    # Earlier chatter in stderr does not hide the fatal last line.
    "Script exited with code 1\nstderr:\nretrying after connection refused\nRuntimeError: dead\nstdout:\n",
])
def test_non_network_text_does_not_match(text):
    from cron.transient_network import is_transient_network_failure

    assert not is_transient_network_failure(text)


def test_agent_declared_failure_prose_is_never_network_class():
    from cron.transient_network import is_transient_network_failure

    assert not is_transient_network_failure("the portal said connection refused", agent_declared=True)


def test_fourteen_dns_failures_in_one_tick_deliver_nothing_and_leave_no_streak(store):
    deliveries = []
    ids = [f"dns-job-{i}" for i in range(14)]
    cron_jobs.save_jobs([_job(id=i, name=i) for i in ids])
    for job_id in ids:
        _tick(job_id, store, deliveries, error="ConnectError: [Errno -2] Name or service not known")
    assert deliveries == []
    stored = {j["id"]: j for j in cron_jobs.load_jobs()}
    assert all(int(stored[i].get("failure_streak") or 0) == 0 for i in ids)
    assert all("failing_since" not in stored[i] for i in ids)
    assert stored[ids[0]]["last_status"] == "error"
    kinds = {inc["failure_type"] for inc in incidents.list_incidents()}
    assert kinds == {"transient_network"}
    assert incidents.count_incidents() == 14


def test_network_failures_do_not_walk_a_job_toward_its_alert_threshold(store):
    deliveries = []
    cron_jobs.save_jobs([_job(alert_after_consecutive_failures=2)])
    for _ in range(5):
        _tick("alert-policy-test", store, deliveries, error="Could not resolve host: x.invalid")
    assert deliveries == []
    # A real failure still counts from zero and alerts at its own threshold.
    _tick("alert-policy-test", store, deliveries, error="mail provider 503")
    assert deliveries == []
    _tick("alert-policy-test", store, deliveries, error="mail provider 503")
    assert len(deliveries) == 1 and deliveries[0][1] is True


def test_non_network_failure_still_alerts_exactly_as_before(store):
    deliveries = []
    cron_jobs.save_jobs([_job()])
    _tick("alert-policy-test", store, deliveries, error="mail provider 503")
    assert len(deliveries) == 1 and deliveries[0][1] is True
    assert incidents.list_incidents()[0]["failure_type"] != "transient_network"


_REFUSALS = [
    "Your request was not processed. Send it again if you still want me to carry it out.",
    "  This turn did not complete. Some actions may already have run; verify their effects before resending.\n",
    "This message was refused by the model's safety filter — try a different query.",
]


@pytest.mark.parametrize("answer", _REFUSALS)
def test_refusal_notice_as_final_response_is_a_failed_run_never_the_message(store, answer):
    deliveries = []
    cron_jobs.save_jobs([_job(alert_after_consecutive_failures=2)])
    _tick("alert-policy-test", store, deliveries, answer=answer)
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert stored["last_status"] == "error" and stored["failure_streak"] == 1
    assert not any(answer.strip() in content for content, _ in deliveries), "the refusal is not the message"
    assert deliveries == [], "below the threshold: nothing delivered"
    _tick("alert-policy-test", store, deliveries, answer=answer)
    assert len(deliveries) == 1 and deliveries[0][1] is True, "thresholds apply: the failure lane alerts"
    assert "provider refusal" in deliveries[0][0], "it reads as a failure notice, not as the job's message"


def test_report_that_quotes_a_refusal_notice_stays_a_report(store):
    deliveries = []
    cron_jobs.save_jobs([_job()])
    text = "Summary: the assistant said 'Your request was not processed.' once last night."
    _tick("alert-policy-test", store, deliveries, answer=text)
    assert deliveries == [(text, False)]


def test_script_keyerror_with_network_stdout_alerts_normally(store):
    deliveries = []
    cron_jobs.save_jobs([_job()])
    _tick("alert-policy-test", store, deliveries,
          error="Script exited with code 1\nstderr:\nKeyError: 'x'\nstdout:\nconnection reset by peer\n")
    assert len(deliveries) == 1 and deliveries[0][1] is True
    assert incidents.list_incidents()[0]["failure_type"] != "transient_network"


# Tick-level outage check (cron.network_probe): an offline box makes any failure network-class.
_OFFLINE_SHAPES = [
    "push refused (HTTP no answer)", "refresh failed (exit 1)",
    "Script timed out after 180s: /opt/data/scripts/vigil.sh",
]


@pytest.mark.parametrize("message", _OFFLINE_SHAPES)
def test_offline_box_makes_any_failure_network_class(store, monkeypatch, message):
    from cron import network_probe

    monkeypatch.setattr(network_probe, "_probe_offline", lambda: True)
    deliveries = []
    cron_jobs.save_jobs([_job(alert_after_consecutive_failures=1)])
    _tick("alert-policy-test", store, deliveries, error=message)
    assert deliveries == []
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert int(stored.get("failure_streak") or 0) == 0 and "failing_since" not in stored
    assert stored["last_status"] == "error" and stored["network_failing_since"]
    assert {inc["failure_type"] for inc in incidents.list_incidents()} == {"transient_network"}


@pytest.mark.parametrize("message", _OFFLINE_SHAPES)
def test_online_box_alerts_for_the_same_messages(store, message):
    deliveries = []
    cron_jobs.save_jobs([_job()])
    _tick("alert-policy-test", store, deliveries, error=message)
    assert len(deliveries) == 1 and deliveries[0][1] is True
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert "network_failing_since" not in stored


def test_probe_runs_once_for_a_tick_of_failing_jobs(store, monkeypatch):
    from cron import network_probe

    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or True)
    deliveries = []
    ids = [f"probe-job-{i}" for i in range(14)]
    cron_jobs.save_jobs([_job(id=i, name=i) for i in ids])
    for job_id in ids:
        _tick(job_id, store, deliveries, error="refresh failed (exit 1)")
    assert deliveries == [] and len(probes) == 1


def test_probe_is_offline_only_when_no_target_answers(monkeypatch):
    from cron import network_probe

    answers = {"api.telegram.org": False, "github.com": True}
    monkeypatch.setattr(network_probe, "_reachable", lambda host, port: answers[host])
    assert network_probe.box_is_offline() is False, "one reachable target is online"
    network_probe.reset_cache()
    answers["github.com"] = False
    assert network_probe.box_is_offline() is True
    answers["github.com"] = True
    assert network_probe.box_is_offline() is True, "the answer is cached for the tick"
    network_probe.reset_cache()
    assert network_probe.box_is_offline() is False


def test_agent_declared_failure_is_not_classified_by_the_probe(monkeypatch):
    from cron import network_probe

    monkeypatch.setattr(network_probe, "_probe_offline", lambda: True)
    assert sched._is_transient_network_run({"id": "j"}, "refresh failed (exit 1)")
    assert not sched._is_transient_network_run({"id": "j"}, "the portal is down", agent_declared=True)


def test_seven_hours_of_network_failures_alert_exactly_once(store):
    deliveries = []
    cron_jobs.save_jobs([_job()])
    err = "Could not resolve host: x.invalid"
    _tick("alert-policy-test", store, deliveries, error=err)
    assert deliveries == []
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert stored["network_failing_since"], "the first network-class failure stamps the clock"
    for _ in range(3):
        _tick("alert-policy-test", store, deliveries, error=err)
    assert deliveries == [], "inside the window: silent"
    jobs = cron_jobs.load_jobs()
    for j in jobs:
        j["network_failing_since"] = (_hermes_now() - timedelta(hours=7)).isoformat()
    cron_jobs.save_jobs(jobs)
    for _ in range(3):
        _tick("alert-policy-test", store, deliveries, error=err)
    assert len(deliveries) == 1 and deliveries[0][1] is True, "one ping, then the incident cooldown holds"
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert stored["failure_streak"] == 3, "past the window the failures count like any other"
    _tick("alert-policy-test", store, deliveries)
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert "network_failing_since" not in stored and stored["failure_streak"] == 0


def test_exhausted_unreachable_ladder_alerts_but_a_pending_rung_does_not(store):
    import socket

    deliveries = []
    err = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    cron_jobs.save_jobs([_job()])
    _tick("alert-policy-test", store, deliveries, error=err)
    assert deliveries == [], "a re-run is pending: the notice is held, nothing counts"
    stored = next(j for j in cron_jobs.load_jobs() if j["id"] == "alert-policy-test")
    assert int(stored.get("failure_streak") or 0) == 0

    cron_jobs.save_jobs([_job(unreachable_retry={"attempt": 3, "at": None, "expr": None})])
    _tick("alert-policy-test", store, deliveries, error=err)
    assert len(deliveries) == 1 and deliveries[0][1] is True, "ladder exhausted: an ordinary alert"


def test_exhausted_unreachable_ladder_alerts_even_when_the_box_is_offline(store, monkeypatch):
    import socket
    from cron import network_probe

    monkeypatch.setattr(network_probe, "_probe_offline", lambda: True)
    deliveries = []
    err = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    cron_jobs.save_jobs([_job(unreachable_retry={"attempt": 3, "at": None, "expr": None})])
    _tick("alert-policy-test", store, deliveries, error=err)
    assert len(deliveries) == 1 and deliveries[0][1] is True, "exhausted: the offline probe is not consulted"


def test_crash_path_is_not_silenced_by_a_stale_unreachable_flag(store):
    sent = []
    job = _job(_model_unreachable=True)
    with patch.object(sched, "_deliver_result", side_effect=lambda jb, content, **kw: sent.append(content)):
        err, outcome = sched._deliver_crash_failure(
            dict(job), "OperationalError: database is locked", adapters={}, loop=None)
        assert outcome != "suppressed" and len(sent) == 1
        err, outcome = sched._deliver_crash_failure(
            dict(job, id="other"), "ConnectError: all connection attempts failed", adapters={}, loop=None)
        assert (err, outcome) == (None, "suppressed") and len(sent) == 1


def test_explicit_failure_type_updates_an_existing_incident(store):
    incident_id, _ = incidents.upsert_incident("job-1", "boom 503")
    kept = incidents.get_incident(incident_id)["failure_type"]
    incidents.upsert_incident("job-1", "boom 503")
    assert incidents.get_incident(incident_id)["failure_type"] == kept, "no explicit type: unchanged"
    incidents.upsert_incident("job-1", "boom 503", failure_type="transient_network")
    assert incidents.get_incident(incident_id)["failure_type"] == "transient_network"
    incidents.upsert_incident("job-1", "boom 503")
    assert incidents.get_incident(incident_id)["failure_type"] == "transient_network"
