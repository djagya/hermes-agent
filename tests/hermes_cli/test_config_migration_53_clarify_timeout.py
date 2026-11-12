"""dlz: config v53 never lowers an explicit ``agent.clarify_timeout``.

Upstream's 52 -> 53 step rewrites a saved 3600 (the old default) to 90. A value in config.yaml cannot be
told apart from a seeded copy of the old default, and the fork's identity config sets 3600 on purpose
(Sera waits up to an hour for the owner's answer; the identity sync re-stamps an older version, so the
step would run again on every sync). The fork keeps the step as a version stamp that rewrites nothing;
an unset key still inherits the new 90-second default at read time.
"""

import hermes_yaml as yaml


def _migrate(tmp_path, monkeypatch, agent):
    from hermes_cli.config_migrations import run_migrations

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"_config_version": 52, "agent": agent}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    results = {"env_added": [], "config_added": [], "warnings": []}
    run_migrations(52, results, quiet=True)
    return yaml.safe_load(config_path.read_text(encoding="utf-8-sig")), results


def test_v53_keeps_an_explicit_hour_long_clarify_timeout(tmp_path, monkeypatch):
    raw, results = _migrate(tmp_path, monkeypatch, {"clarify_timeout": 3600, "max_turns": 90})
    assert raw["agent"] == {"clarify_timeout": 3600, "max_turns": 90}
    assert "agent.clarify_timeout=90" not in results["config_added"]


def test_v53_unset_clarify_timeout_follows_the_new_default(tmp_path, monkeypatch):
    from hermes_cli.config import DEFAULT_CONFIG

    raw, _ = _migrate(tmp_path, monkeypatch, {"max_turns": 90})
    assert "clarify_timeout" not in raw["agent"]
    assert DEFAULT_CONFIG["agent"]["clarify_timeout"] == 90
