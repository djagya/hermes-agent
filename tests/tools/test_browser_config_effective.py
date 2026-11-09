"""``browser.*`` reads go through the effective user config: the managed overlay wins over the profile's
file and ``${VAR}`` references expand, as for every other runtime reader (hermes_cli/AGENTS.md, Three
loaders). A raw read would launch Chromium on a literal ``${HOME}/...`` dir under HERMES_HOME, or ignore
an administrator's pinned profile and flags.

dlz: on the fork the managed layer is a SEED (``bec93f287d``, ``managed_scope.apply_managed_overlay``):
it fills leaves the user file omits, and a present user leaf wins. So each managed case below leaves
the key out of the user file (a raw read would then miss the managed value), and
``test_managed_profile_pin_and_flags_seed_the_user_file`` also pins that a present user leaf wins."""

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
    for name in ("BROWSER_USE_API_KEY", "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    _reset()
    yield home, managed
    _reset()


def _reset():
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope
    from tools import browser_session_cloud

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()
    browser_session_cloud.reset_caches()


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    _reset()


def test_user_data_dir_and_chromium_args_expand_env_references(homes, tmp_path, monkeypatch):
    from tools.browser_session_cloud import chromium_args, user_data_dir

    home, _managed = homes
    monkeypatch.setenv("FIXTURE_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("FIXTURE_LANG", "de")
    _write(home / "config.yaml", """
        browser:
          user_data_dir: ${FIXTURE_PROFILE_ROOT}/chrome-profile
          chromium_args: ["--lang=${FIXTURE_LANG}"]
        """)
    assert user_data_dir() == str(tmp_path / "profiles" / "chrome-profile")
    assert chromium_args() == ["--lang=de"]


def test_managed_profile_pin_and_flags_seed_the_user_file(homes, tmp_path):
    from tools.browser_session_cloud import chromium_args, user_data_dir

    home, managed = homes
    _write(managed / "config.yaml", f"""
        browser:
          user_data_dir: {tmp_path / "pinned"}
          chromium_args: ["--proxy-server=http://proxy.corp:3128"]
        """)
    _write(home / "config.yaml", f"""
        browser:
          user_data_dir: {tmp_path / "mine"}
        """)
    assert user_data_dir() == str(tmp_path / "mine")  # dlz: a present user leaf wins over the seed
    assert chromium_args() == ["--proxy-server=http://proxy.corp:3128"]  # omitted leaf: seeded


def test_managed_local_cloud_provider_reaches_the_cloud_provider_read(homes, monkeypatch):
    """The cloud-provider read is a ``browser.*`` read too: with Browserbase credentials in the env, a
    managed ``local`` keeps pages off the cloud when the user file does not choose a provider."""
    from tools.browser_session_cloud import get_cloud_provider

    home, managed = homes
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "bb-project")
    _write(home / "config.yaml", """
        browser:
          headless: true
        """)
    _write(managed / "config.yaml", """
        browser:
          cloud_provider: local
        """)
    assert get_cloud_provider() is None


def test_browser_exec_cloud_routing_reads_the_managed_gateway_selection(homes):
    """``browser_exec``'s own ``browser:`` read (Browser Use direct API vs the Nous gateway) sees the managed
    selection the session layer sees."""
    from tools import browser_use_cli

    home, managed = homes
    _write(home / "config.yaml", """
        browser:
          headless: true
        """)
    _write(managed / "config.yaml", """
        browser:
          cloud_provider: nous
        """)
    assert browser_use_cli._use_gateway(browser_use_cli._read_browser_cfg()) is True


def test_real_profile_pin_follows_the_managed_overlay(homes):
    """The real-profile copy's source pin is a ``browser.*`` read too: a managed pin decides which identity
    the agent gets."""
    from hermes_cli.browser_connect import _real_profile_pin

    home, managed = homes
    _write(home / "config.yaml", """
        browser:
          headless: true
        """)
    _write(managed / "config.yaml", """
        browser:
          real_profile_pin: Default
        """)
    assert _real_profile_pin() == "Default"


def test_browser_status_reports_the_cdp_url_the_browser_tool_reads(homes, monkeypatch):
    """``/browser status`` shows what the next browser call connects to: the managed ``browser.cdp_url``."""
    from tui_gateway import server

    home, managed = homes
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    _write(home / "config.yaml", """
        browser:
          headless: true
        """)
    _write(managed / "config.yaml", """
        browser:
          cdp_url: http://managed-host:9222
        """)
    resp = server.handle_request({"id": "1", "method": "browser.manage", "params": {"action": "status"}})
    assert resp["result"]["url"] == "http://managed-host:9222"
