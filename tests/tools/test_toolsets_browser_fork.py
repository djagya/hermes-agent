"""dlz: the browser toolset keeps the fork's managed-slot tool (B4) next to upstream's browser tools."""

from toolsets import _HERMES_CORE_TOOLS, resolve_toolset

FORK_BROWSER = {"browser_exec", "browser_cdp", "browser_dialog", "browser_vault", "browser_control"}


def test_browser_toolset_carries_every_fork_browser_tool():
    assert FORK_BROWSER <= set(resolve_toolset("browser"))
    assert FORK_BROWSER <= set(_HERMES_CORE_TOOLS)


def test_every_browser_tool_is_registered():
    # The polymarket recovery cron imports tools.browser_control_tool; a missing module is a dead tool.
    from tools.registry import discover_builtin_tools, registry
    discover_builtin_tools()
    assert {name for name in FORK_BROWSER if registry.get_entry(name) is None} == set()
