"""dlz: the bundled homeassistant plugin loads like a core platform in a CLI/TUI process: discovered
from plugins/homeassistant without a plugins.enabled entry, adapter deferred, ha_* tools registered."""

HA_TOOLS = {"ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service"}


def test_bundled_homeassistant_defers_the_adapter_and_registers_its_tools(monkeypatch):
    monkeypatch.delenv("HERMES_BUNDLED_PLUGINS", raising=False)
    from hermes_cli.plugins import PluginManager
    from toolsets import resolve_toolset

    mgr = PluginManager()
    mgr.discover_and_load()

    ha = mgr._plugins.get("homeassistant")
    assert ha is not None, "bundled homeassistant plugin was not discovered"
    assert ha.manifest.source == "bundled"
    assert ha.deferred is True
    assert set(resolve_toolset("homeassistant")) == HA_TOOLS
    assert HA_TOOLS.issubset(set(resolve_toolset("hermes-homeassistant")))


def test_bundled_homeassistant_materializes_as_the_homeassistant_platform(monkeypatch):
    """A gateway resolves the deferred loader on first use and gets the plugin's adapter, with the
    fork's cold-boot grace (plugins/homeassistant/adapter.py, VENDORED.json fork_patch)."""
    monkeypatch.delenv("HERMES_BUNDLED_PLUGINS", raising=False)
    from gateway.config import PlatformConfig
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import PluginManager

    PluginManager().discover_and_load()
    entry = platform_registry.get("homeassistant")
    assert entry is not None and entry.required_env == ["HASS_TOKEN"]
    adapter = platform_registry.create_adapter(
        "homeassistant", PlatformConfig(enabled=True, token="tok", extra={"url": "http://ha.invalid:8123"}))
    assert type(adapter).__name__ == "HomeAssistantAdapter"
    assert type(adapter)._BOOT_GRACE_SECONDS == 180.0
