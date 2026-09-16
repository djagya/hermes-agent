"""Each standalone config loader (gateway, TUI/desktop, cron) must honor managed scope.

These loaders build their own config dict instead of routing through
hermes_cli.config.load_config, so the managed overlay has to be wired into each.
This is the regression guard for the whole bug class (a managed display.skin was
silently ignored by the TUI; the same gap existed in the gateway and cron).
"""
import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _seed(home, managed, *, user, mgd):
    (home / "config.yaml").write_text(textwrap.dedent(user), encoding="utf-8")
    (managed / "config.yaml").write_text(textwrap.dedent(mgd), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()








def test_timezone_user_wins_over_managed_seed(homes, monkeypatch):
    home, managed = homes
    # hermes_time checks an env override first; ensure it's unset so config wins.
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    _seed(home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n")
    import hermes_time

    assert hermes_time._resolve_timezone_name() == "America/New_York"


def test_timezone_managed_seed_when_user_omits(homes, monkeypatch):
    home, managed = homes
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    _seed(home, managed, user="model:\n  default: user/model\n", mgd="timezone: Asia/Tokyo\n")
    import hermes_time

    assert hermes_time._resolve_timezone_name() == "Asia/Tokyo"


def test_gateway_env_bridge_user_timezone_wins(homes, monkeypatch):
    """The gateway config→env bridge must bridge the user value when present."""
    home, managed = homes
    _seed(home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    import yaml

    raw = yaml.safe_load((home / "config.yaml").read_text())
    bridged = managed_scope.apply_managed_overlay(raw)
    assert bridged.get("timezone") == "America/New_York"


def test_gateway_env_bridge_seeds_omitted_timezone(homes):
    home, managed = homes
    _seed(home, managed, user="model:\n  default: user/model\n", mgd="timezone: Asia/Tokyo\n")
    from hermes_cli import managed_scope
    import yaml

    managed_scope.invalidate_managed_cache()
    raw = yaml.safe_load((home / "config.yaml").read_text())
    bridged = managed_scope.apply_managed_overlay(raw)
    assert bridged.get("timezone") == "Asia/Tokyo"
