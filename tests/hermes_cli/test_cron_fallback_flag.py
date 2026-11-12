"""``hermes cron create/edit --fallback PROVIDER:MODEL`` / ``--clear-fallback`` (per-job fallbacks)."""

import argparse

import pytest

from cron.jobs import create_job, get_job
from hermes_cli.cron import _job_rows, cron_command
from hermes_cli.subcommands.cron import build_cron_parser


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _parse(*argv):
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    return parser.parse_args(["cron", *argv])


def test_cron_edit_fallback_sets_list_in_order_and_persists(tmp_cron_dir, capsys):
    job = create_job(prompt="p", schedule="every 1h", provider="zai", model="glm-5.3")
    cron_command(_parse("edit", job["id"], "--fallback", "openrouter:z-ai/glm-5.2",
                        "--fallback", "xai:grok-4.5"))
    assert get_job(job["id"])["fallback_providers"] == [
        {"provider": "openrouter", "model": "z-ai/glm-5.2"},
        {"provider": "xai", "model": "grok-4.5"}]
    out = capsys.readouterr().out
    assert "Updated job" in out and "Fallback: openrouter:z-ai/glm-5.2, xai:grok-4.5" in out


def test_cron_edit_fallback_replaces_the_existing_list(tmp_cron_dir):
    job = create_job(prompt="p", schedule="every 1h", provider="zai", model="glm-5.3",
                     fallback_providers=[{"provider": "xai", "model": "grok-4.5"}])
    cron_command(_parse("edit", job["id"], "--fallback", "openrouter:m"))
    assert get_job(job["id"])["fallback_providers"] == [{"provider": "openrouter", "model": "m"}]


def test_cron_edit_clear_fallback_removes_the_key(tmp_cron_dir):
    job = create_job(prompt="p", schedule="every 1h", provider="zai", model="glm-5.3",
                     fallback_providers=[{"provider": "xai", "model": "grok-4.5"}])
    cron_command(_parse("edit", job["id"], "--clear-fallback"))
    assert "fallback_providers" not in get_job(job["id"])


def test_cron_edit_without_fallback_flags_leaves_the_list_alone(tmp_cron_dir):
    job = create_job(prompt="p", schedule="every 1h", fallback_providers=[{"provider": "xai", "model": "m"}])
    cron_command(_parse("edit", job["id"], "--name", "renamed"))
    assert get_job(job["id"])["fallback_providers"] == [{"provider": "xai", "model": "m"}]


def test_cron_create_accepts_fallback(tmp_cron_dir, monkeypatch):
    monkeypatch.setattr("hermes_cli.cron._warn_if_gateway_not_running", lambda: None)
    monkeypatch.setattr("cron.scheduler.create_job_with_scheduler_registration",
                        lambda **kw: create_job(**kw))
    cron_command(_parse("create", "every 1h", "hi", "--provider", "zai", "--model", "glm-5.3",
                        "--fallback", "openrouter:z-ai/glm-5.2"))
    (job,) = [j for j in __import__("cron.jobs", fromlist=["list_jobs"]).list_jobs()]
    assert job["fallback_providers"] == [{"provider": "openrouter", "model": "z-ai/glm-5.2"}]


@pytest.mark.parametrize("bad", ["openrouter", ":model", "openrouter:", " : "])
def test_cron_edit_fallback_flag_rejects_malformed_spec(bad, capsys):
    with pytest.raises(SystemExit):
        _parse("edit", "j", "--fallback", bad)
    assert "use PROVIDER:MODEL" in capsys.readouterr().err


def test_cron_edit_fallback_and_clear_fallback_are_exclusive():
    with pytest.raises(SystemExit):
        _parse("edit", "j", "--fallback", "a:b", "--clear-fallback")


@pytest.mark.parametrize("bad, message", [
    ("nope", "must be a list"),
    (["x"], "must be an object"),
    ([{"provider": "a"}], "non-empty provider and model"),
    ([{"provider": "a", "model": "m", "api_key": "sk-x"}], "unsupported key"),
])
def test_cron_edit_rejects_invalid_fallback_entries_and_stores_nothing(tmp_cron_dir, bad, message):
    from tools.cronjob_tools import cronjob
    import json
    job = create_job(prompt="p", schedule="every 1h")
    result = json.loads(cronjob(action="update", job_id=job["id"], fallback_providers=bad))
    assert result["success"] is False and message in result["error"]
    assert "fallback_providers" not in get_job(job["id"])


def test_cron_edit_refuses_fallback_with_off_origin_base_url(tmp_cron_dir):
    from tools.cronjob_tools import cronjob
    import json
    job = create_job(prompt="p", schedule="every 1h")
    result = json.loads(cronjob(action="update", job_id=job["id"], fallback_providers=[
        {"provider": "openrouter", "model": "m", "base_url": "https://attacker.example/v1"}]))
    assert result["success"] is False and "fallback_providers openrouter:m" in result["error"]
    assert "fallback_providers" not in get_job(job["id"])


def test_cron_list_rows_show_the_fallback_list(tmp_cron_dir):
    job = create_job(prompt="p", schedule="every 1h", provider="zai", model="glm-5.3",
                     fallback_providers=[{"provider": "xai", "model": "grok-4.5"}])
    assert ("Fallback", "xai:grok-4.5") in _job_rows(get_job(job["id"]))
    plain = create_job(prompt="q", schedule="every 1h")
    assert "Fallback" not in dict(_job_rows(get_job(plain["id"])))
