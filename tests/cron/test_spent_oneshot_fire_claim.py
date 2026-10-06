"""A one-shot whose dispatch budget is spent must not wedge its fire claim.

Live incident shape: a paused one-shot with ``repeat.completed >= repeat.times`` was
rescheduled + resumed, then run manually. The manual claim stamped ``fire_claim``, the
run body's ``claim_dispatch`` rejected the spent budget ("Dispatch claim rejected;
execution was not started") and retired the record, but nothing released the claim.

Real store under a temp HERMES_HOME; only the agent run itself is stubbed.
"""
import pytest

import cron.scheduler as scheduler


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _spent_oneshot():
    from cron.jobs import _hermes_now, create_job, update_job

    job = create_job(prompt="x", schedule="in 30m", name="spent")
    # Earlier runs completed (last_run_at set) and spent the budget; the record was then
    # rescheduled, so it is active again with nothing left to dispatch.
    update_job(job["id"], {
        "repeat": {"times": 1, "completed": 4}, "last_run_at": _hermes_now().isoformat()})
    return job["id"]


def test_manual_and_forced_fire_refuse_a_spent_oneshot_without_claiming(temp_home):
    from cron.jobs import claim_job_for_fire, get_job

    jid = _spent_oneshot()

    assert claim_job_for_fire(jid, manual=True) is False
    assert claim_job_for_fire(jid, force=True) is False
    assert get_job(jid).get("fire_claim") is None


def test_tick_claim_on_spent_oneshot_retires_it_and_releases_the_claim(temp_home, monkeypatch):
    """The ticker still claims (so claim_dispatch retires the record), and the rejected fire
    hands its claim back instead of leaving it on the completed job."""
    from cron.jobs import claim_job_for_fire, get_job

    jid = _spent_oneshot()
    claimed = claim_job_for_fire(jid, return_job=True)
    assert isinstance(claimed, dict) and claimed["fire_claim"]

    ran = []
    monkeypatch.setattr(scheduler, "run_job", lambda *a, **kw: ran.append(a))

    assert scheduler.run_one_job(claimed) is True

    after = get_job(jid)
    assert ran == []
    assert after["state"] == "completed"
    assert after["enabled"] is False
    assert after.get("fire_claim") is None


def test_release_fire_claim_is_owner_fenced(temp_home):
    from cron.jobs import claim_job_for_fire, create_job, get_job, release_fire_claim

    jid = create_job(prompt="x", schedule="every 5m", name="r")["id"]
    claim = claim_job_for_fire(jid, return_job=True)["fire_claim"]

    assert release_fire_claim(jid, expected_owner="someone-else:1:x") is False
    assert get_job(jid)["fire_claim"] == claim
    assert release_fire_claim(jid, expected_owner=claim["by"]) is True
    assert get_job(jid)["fire_claim"] is None


def test_manual_run_of_spent_oneshot_names_the_rearm_route(temp_home):
    from tools.cronjob_tools import _claim_for_manual_run

    jid = _spent_oneshot()
    claimed, err = _claim_for_manual_run(jid, "immediate run")

    assert claimed is None
    assert err["claimed"] is False
    assert "4/1" in err["error"]
    assert f"cron resume {jid} --run-now" in err["error"]


def test_rearmed_oneshot_is_manually_runnable_again(temp_home):
    """The named route really restores runnability (re-arm resets the spent budget)."""
    from cron.jobs import _hermes_now, claim_job_for_fire, rearm_oneshot

    jid = _spent_oneshot()
    rearm_oneshot(jid, _hermes_now().isoformat())

    assert claim_job_for_fire(jid, manual=True) is True
