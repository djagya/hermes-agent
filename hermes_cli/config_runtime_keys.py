"""Config keys the runtime reads that the DEFAULT_CONFIG walk in ``_validate_config_key`` cannot see.

``_validate_config_key`` asks here only AFTER the schema walk refused a key, so nothing here can
override a schema verdict; it only vouches for an exact path (and what is below it) a reader uses.
Four owners supply them:

* Core selection keys (``browser.cloud_provider``, ``<category>.use_gateway``, ``stt.provider``):
  :func:`tools.tool_backend_helpers.selection_config_keys`, built from ``read_selection``'s tables.
* Core-owned unseeded keys outside that table, each listed with its reader (:data:`CORE_RUNTIME_KEYS`).
* MoA preset fields under any preset name (:func:`hermes_cli.moa_config.is_preset_field_path`). Only a
  preset's own fields and its aggregator slot fields qualify, not arbitrary descendants of ``moa.presets``.
* ACTIVE plugins: a ``config_keys`` list in ``plugin.yaml`` (or ``extensions.com.nousresearch.hermes``
  in a portable ``plugin.json``) names core-section paths the plugin's code reads, e.g.
  ``memory.retaindb``. Plugins are found and gated by the loader's own discovery
  (``plugins_discovery``), plus the memory provider ``memory.provider`` selects. A declared path is
  at least two segments deep, is ignored when DEFAULT_CONFIG already defines it, and covers itself
  and everything below it.

Nothing is cached: the lookup runs only for a key the schema refused, and a plugin installed or
enabled in a long-running process must count on the next ``config get/set``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator

logger = logging.getLogger(__name__)

# Core-owned keys read at runtime but deliberately absent from DEFAULT_CONFIG -> their reader.
CORE_RUNTIME_KEYS: dict[str, str] = {
    "computer_use.backend": "plugins/computer_use/__init__.py configured_backend_name() (unset = cua)",
    "dashboard.kanban": "plugins/kanban/dashboard/plugin_api.py (bundled dashboard tab preferences)",
    "dashboard.font": "hermes_cli/web_routers/dashboard_ui.py get_dashboard_font() (unset = theme)",
    "dashboard.hidden_plugins": "hermes_cli/web_routers/dashboard_ui.py get_dashboard_plugins() (unset = none)",
    "agent.prefill_messages_file": "gateway/run_config_loaders.py _load_prefill_messages() (unset = none)",
    "agent.system_prompt": "hermes_cli/cli_commands_mixin.py _handle_personality_command() (unset = empty)",
    "database.synchronous": "hermes_state_wal.py apply_database_pragmas() (unset = the built-in pragma)",
    "display.busy_text_mode": "gateway/run_config_loaders.py _busy_modes_from_config() (unset = fallback)",
    "display.tool_progress": "hermes_cli/setup.py, gateway/session_tui_config.py (unset = per-platform default)",
    "gateway.standalone": "hermes_cli/profiles.py profile_is_standalone() (unset = not standalone)",
    "terminal.credential_files": "tools/credential_files.py _load_config_files() (unset = none)",
    # dlz: fork-owned keys DEFAULT_CONFIG leaves unseeded (config_defaults.py is at its line cap). Absent keeps
    # each reader's own default, so listing them here changes no runtime behaviour.
    "cron.quiet_network_failures": "cron/scheduler.py _quiet_network_failures() (unset = off; literal true only)",
    "cron.retry_unreachable": "cron/unreachable_retry.py retry_enabled() (unset = on; literal false only)",
    "cron.cleanup_timeout_seconds": "cron/scheduler.py _cron_cleanup_timeout_seconds() (unset = 10)",
    "cron.misfire_grace_minutes": "cron/scheduler_provider.py _misfire_grace_minutes() (unset = 10)",
    "cron.standalone_send_timeout_seconds": "cron/scheduler_delivery.py (unset = 60)",
    "vault.payment_pay_once": "tools/payment_consent_prompt.py payment_pay_once_enabled() (unset = on)",
    "vault.payment_fill_auto": "agent/vault_payment_policy.py payment_fill_auto_enabled() (unset = on)",
    "vault.payment_repin": "agent/vault_payment_policy.py payment_repin_enabled() (unset = off; literal true only)",
    "vault.payment_providers": "agent/vault_payment_policy.py enabled_providers() (unset = the default providers)",
    "vault.payment_consent_strict_exposure": "agent/vault_payment_policy.py destination_policy() (unset = off)",
    "vault.letter_intake.enabled": "agent/vault_letter.py letter_intake_enabled() (unset = intake off)",
    "vault.letter_intake.ttl_hours": "agent/vault_letter.py _ttl_seconds() (unset = the default TTL)",
}


def _covered(key: str, paths: Iterable[str]) -> bool:
    return any(key == path or key.startswith(path + ".") for path in paths)


def _in_default_config(path: str) -> bool:
    from hermes_cli.config import DEFAULT_CONFIG

    node = DEFAULT_CONFIG
    for segment in path.split("."):
        if not isinstance(node, dict) or segment not in node:
            return False
        node = node[segment]
    return True


def _selected_memory_provider_manifest():
    """Manifest of the memory provider ``memory.provider`` selects. Memory providers are ``exclusive``
    plugins: the general gate never enables them, the selection does."""
    from hermes_cli.config import cfg_get, load_config
    from hermes_cli.plugins_manifest import parse_manifest_file

    name = cfg_get(load_config(), "memory", "provider")
    if not isinstance(name, str) or not name.strip():
        return None
    from plugins.memory import _explicitly_disabled, find_provider_dir

    provider_dir = find_provider_dir(name.strip())
    # The memory loader refuses a provider parked in ``plugins.disabled``; its keys are not live either.
    if provider_dir is None or _explicitly_disabled(name.strip(), provider_dir):
        return None
    manifest_file = next((f for f in (provider_dir / "plugin.yaml", provider_dir / "plugin.yml") if f.exists()), None)
    return parse_manifest_file(manifest_file, provider_dir, "memory", "memory") if manifest_file else None


def _active_manifests() -> Iterator:
    """Manifests of the plugins the loader would activate, by its own discovery, precedence and gate."""
    from hermes_cli.plugins_discovery import (
        _get_disabled_plugins, _get_enabled_plugins, collect_directory_manifests, discover_entrypoint_manifests,
        gate_manifest, resolve_manifest_winners,
    )
    from hermes_cli.plugins_manifest import manifest_key, requires_hermes_error

    manifests = collect_directory_manifests()
    # Same merge as PluginManager._discover_and_load_inner: a directory install keeps its identity.
    directory_keys = {manifest_key(m) for m in manifests}
    manifests += [m for m in discover_entrypoint_manifests() if manifest_key(m) not in directory_keys]
    disabled, enabled = _get_disabled_plugins(), _get_enabled_plugins()
    for manifest in resolve_manifest_winners(manifests).values():
        verdict = gate_manifest(manifest, disabled, enabled)
        if (verdict.action != "placeholder" or verdict.enabled) and requires_hermes_error(manifest) is None:
            yield manifest
    memory = _selected_memory_provider_manifest()
    if memory is not None:
        yield memory


def plugin_declared_config_keys() -> frozenset[str]:
    """``config_keys`` declared by active plugins, minus paths DEFAULT_CONFIG already defines."""
    keys = set()
    for manifest in _active_manifests():
        for path in manifest.config_keys:
            if _in_default_config(path):
                logger.debug("config_keys: ignoring %s from plugin %s (a DEFAULT_CONFIG key)", path, manifest.name)
            else:
                keys.add(path)
    return frozenset(keys)


def is_runtime_read_key(parts: list[str]) -> bool:
    """True when the split key ``parts`` is a MoA preset field, a core selection key, a core runtime key,
    or sits at/under a path an active plugin declares. Call only for a key the schema walk refused.

    The MoA rule needs the segments (a preset name may contain a dot); the other owners match the
    dotted join."""
    from hermes_cli.moa_config import is_preset_field_path
    from tools.tool_backend_helpers import selection_config_keys

    if is_preset_field_path(parts):
        return True
    dotted = ".".join(parts)
    if dotted in selection_config_keys() or _covered(dotted, CORE_RUNTIME_KEYS):
        return True
    return _covered(dotted, plugin_declared_config_keys())
