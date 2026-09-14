"""Managed mode refuses /browser connect and desktop Chrome attach."""

import os

from hermes_cli.browser_connect import (
    MANAGED_CONNECT_REFUSAL,
    launch_chrome_debug,
    managed_connect_refusal,
    try_launch_chrome_debug,
)
from hermes_cli.cli_commands_mixin import CLICommandsMixin


def test_managed_connect_refusal_from_env(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
    assert managed_connect_refusal() == MANAGED_CONNECT_REFUSAL


def test_unmanaged_connect_refusal_is_none(monkeypatch):
    monkeypatch.delenv("HERMES_BROWSER_CONTROL_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
    assert managed_connect_refusal() is None


def test_handle_browser_connect_refuses_without_writing_env(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
    launched = []
    monkeypatch.setattr(
        "hermes_cli.cli_commands_mixin.launch_chrome_debug",
        lambda *a, **k: launched.append(True),
    )
    CLICommandsMixin._handle_browser_command(object(), "/browser connect")
    out = capsys.readouterr().out
    assert MANAGED_CONNECT_REFUSAL in out
    assert not os.environ.get("BROWSER_CDP_URL")
    assert launched == []


def test_launch_chrome_debug_does_not_spawn_when_managed(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.browser_connect.get_chrome_debug_candidates",
        lambda system: ["/bin/chrome"],
    )

    def boom(*args, **kwargs):
        raise AssertionError("must not spawn a desktop Chrome in managed mode")

    monkeypatch.setattr("hermes_cli.browser_connect.subprocess.Popen", boom)
    result = launch_chrome_debug(9222, "Linux")
    assert result.launched is False
    assert try_launch_chrome_debug(9222, "Linux") is False
