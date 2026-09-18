"""Each standalone config loader (gateway, TUI/desktop, cron) must honor managed seeds.

These loaders build their own config dict instead of routing through
hermes_cli.config.load_config, so the managed seed has to be wired into each.
A present user leaf wins; an omitted leaf takes the seed.
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
    from hermes_cli import config_effective, managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _seed(home, managed, *, user, mgd):
    (home / "config.yaml").write_text(textwrap.dedent(user), encoding="utf-8")
    (managed / "config.yaml").write_text(textwrap.dedent(mgd), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def test_timezone_user_leaf_beats_managed_seed(homes, monkeypatch):
    home, managed = homes
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    _seed(home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n")
    import hermes_time

    assert hermes_time._resolve_timezone_name() == "America/New_York"


def test_timezone_omitted_leaf_takes_managed_seed(homes, monkeypatch):
    home, managed = homes
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    _seed(home, managed, user="display:\n  show_reasoning: true\n", mgd="timezone: Asia/Tokyo\n")
    import hermes_time

    assert hermes_time._resolve_timezone_name() == "Asia/Tokyo"


def test_gateway_env_bridge_honors_user_then_seed(homes):
    """The gateway config→env bridge must consume the seeded overlay.

    We assert on the managed-overlaid config the bridge consumes — the
    bridge writes whatever this dict carries.
    """
    home, managed = homes
    _seed(home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n")
    from hermes_cli import managed_scope
    import yaml

    managed_scope.invalidate_managed_cache()
    raw = yaml.safe_load((home / "config.yaml").read_text())
    bridged = managed_scope.apply_managed_overlay(raw)
    assert bridged.get("timezone") == "America/New_York"

    _seed(home, managed, user="display:\n  show_reasoning: true\n", mgd="timezone: Asia/Tokyo\n")
    managed_scope.invalidate_managed_cache()
    raw = yaml.safe_load((home / "config.yaml").read_text())
    bridged = managed_scope.apply_managed_overlay(raw)
    assert bridged.get("timezone") == "Asia/Tokyo"
