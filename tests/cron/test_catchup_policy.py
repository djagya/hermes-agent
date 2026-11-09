"""The local missed-run policy preserves grace and manual triggers."""
from datetime import timedelta

import pytest

from cron import jobs


@pytest.mark.parametrize("catch_up", [True, False])
def test_missed_policy_preserves_grace_and_manual(tmp_path, monkeypatch, catch_up):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"cron:\n  catch_up_missed: {str(catch_up).lower()}\n", encoding="utf-8")
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        for name, lag in [("stale", 14400), ("grace", 30), ("manual", 14400)]:
            job = jobs.create_job(prompt=name, schedule="every 1h", model="fixture", deliver="local")
            stored = jobs.load_jobs()
            row = next(row for row in stored if row["id"] == job["id"])
            row["next_run_at"] = (now - timedelta(seconds=lag)).isoformat()
            if name == "manual":
                row["manual_run_at"] = row["next_run_at"]
            jobs.save_jobs(stored)
        due = {job["prompt"] for job in jobs.get_due_jobs()}
        assert due == ({"stale", "grace", "manual"} if catch_up else {"grace", "manual"})
        stale = next(row for row in jobs.load_jobs() if row["prompt"] == "stale")
        assert jobs._ensure_aware(jobs.datetime.fromisoformat(stale["next_run_at"])) > now


@pytest.mark.parametrize("uncomputable", [False, True])
def test_default_and_uncomputable_still_catch_up(tmp_path, monkeypatch, uncomputable):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if uncomputable:
        (tmp_path / "config.yaml").write_text("cron:\n  catch_up_missed: false\n", encoding="utf-8")
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="default", schedule="every 1h", model="fixture", deliver="local")
        stored = jobs.load_jobs()
        stored[0]["next_run_at"] = (jobs._hermes_now() - timedelta(hours=4)).isoformat()
        jobs.save_jobs(stored)
        if uncomputable:
            monkeypatch.setattr(jobs, "compute_next_run", lambda *args, **kwargs: None)
        assert [row["id"] for row in jobs.get_due_jobs()] == [job["id"]]


@pytest.mark.parametrize(("global_on", "legacy"), [(False, True), (True, False)])
def test_legacy_job_catch_up_missed_still_overrides_config(tmp_path, monkeypatch, global_on, legacy):
    """dlz B9/D04: live jobs still carry the fork's per-job ``catch_up_missed``. Under the box's
    ``cron.catch_up_missed: false`` a legacy ``true`` must still run its one missed slot (and a
    legacy ``false`` still opts out under a true global) until the job is edited onto
    ``catch_up``; that edit drops the legacy key for good."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"cron:\n  catch_up_missed: {str(global_on).lower()}\n", encoding="utf-8")
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="own", schedule="every 1h", model="fixture", deliver="local")
        stored = jobs.load_jobs()
        stored[0]["catch_up_missed"] = legacy  # a record written by the v0.22 fork
        jobs.save_jobs(stored)

        def stale_and_due():
            stored = jobs.load_jobs()
            stored[0]["next_run_at"] = (jobs._hermes_now() - timedelta(hours=4)).isoformat()
            jobs.save_jobs(stored)
            return [row["id"] for row in jobs.get_due_jobs()] == [job["id"]]

        assert stale_and_due() is legacy
        jobs.update_job(job["id"], {"catch_up": not legacy})
        assert "catch_up_missed" not in jobs.load_jobs()[0]
        assert stale_and_due() is (not legacy)
        jobs.update_job(job["id"], {"catch_up": None})
        assert stale_and_due() is global_on


def test_legacy_catch_up_missed_does_not_revive_a_stale_oneshot(tmp_path, monkeypatch):
    """The fork key only ever covered recurring jobs: a past-grace one-shot still retires."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="once", schedule="in 30m", model="fixture", deliver="local")
        stored = jobs.load_jobs()
        stored[0]["catch_up_missed"] = True
        stored[0]["next_run_at"] = (jobs._hermes_now() - timedelta(hours=4)).isoformat()
        jobs.save_jobs(stored)
        assert job["id"] not in [row["id"] for row in jobs.get_due_jobs()]
