"""Cross-surface contract for the persistent /approvals mode command."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from cli import HermesCLI
from hermes_cli.commands import (
    GATEWAY_KNOWN_COMMANDS,
    SUBCOMMANDS,
    gateway_help_lines,
    resolve_command,
)
# SlashCommandCompleter / telegram_bot_commands live in their defining modules;
# importing them through hermes_cli.commands goes through the PLUGIN-COMPAT
# lazy table (scripts/check_compat_pointers.py fails CI on in-tree use).
from hermes_cli.commands_completion import SlashCommandCompleter
from hermes_cli.commands_platforms import telegram_bot_commands
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document


def _completions(text: str) -> set[str]:
    return {
        item.text
        for item in SlashCommandCompleter().get_completions(
            Document(text=text), CompleteEvent(completion_requested=True)
        )
    }


def test_approvals_registry_drives_help_menu_and_autocomplete():
    command = resolve_command("approvals")
    assert command is not None
    assert command.category == "Configuration"
    assert command.args_hint == "[manual|smart|off]"
    assert SUBCOMMANDS["/approvals"] == ["manual", "smart", "off"]
    assert "approvals" in GATEWAY_KNOWN_COMMANDS
    assert any("/approvals" in line for line in gateway_help_lines())
    assert "approvals" in {name for name, _ in telegram_bot_commands()}
    assert _completions("/approvals ") == {"manual", "smart", "off"}


def _isolate_config(monkeypatch, home):
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(home / "missing-managed"))
    from hermes_cli import managed_scope
    from hermes_cli.config import _LOAD_CONFIG_CACHE, _RAW_CONFIG_CACHE

    _LOAD_CONFIG_CACHE.clear()
    _RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()






def test_managed_seed_does_not_lock_approval_mode(tmp_path, monkeypatch):
    """Fork seed semantics (f5405d53f4): the managed file seeds missing leaves
    (``approvals.cron_mode``) but does NOT pin ``approvals.mode`` — a live
    ``/approvals off`` writes the user file and wins. The write lands in the
    user config and nothing refuses it."""
    from hermes_cli import managed_scope
    from hermes_cli.approval_mode import run_approval_mode_command

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (managed / "config.yaml").write_text(
        "approvals:\n  cron_mode: deny\n", encoding="utf-8"
    )
    managed_scope.invalidate_managed_cache()

    result = run_approval_mode_command("off")

    assert result.ok is True
    assert result.mode == "off"
    assert (home / "config.yaml").exists()


def test_managed_seed_still_guards_managed_env_secrets(tmp_path, monkeypatch):
    """The fork keeps refusing managed .env secrets (f5405d53f4 kept that
    refusal); the seed never turns the managed scope into a no-op."""
    from hermes_cli import managed_scope

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (managed / "config.yaml").write_text(
        "approvals:\n  cron_mode: deny\n", encoding="utf-8"
    )
    managed_scope.invalidate_managed_cache()
    # The seed still fills a leaf the user omitted.
    from hermes_cli.config import load_config, cfg_get

    assert cfg_get(load_config(), "approvals", "cron_mode") == "deny"






