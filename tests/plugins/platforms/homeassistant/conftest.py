"""Test bootstrap: import the plugin directory as the package ``homeassistant_plugin``.

dlz: vendored from NousResearch/hermes-homeassistant ``tests/conftest.py`` (see
``plugins/homeassistant/VENDORED.json``). The plugin root is the bundled copy under ``plugins/``,
not this directory, so the repo-root collection workaround upstream needs is dropped.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``gateway``, ``agent``, ...) must be importable — run with a hermes-agent checkout on ``PYTHONPATH``.

The adapter resolves ``Platform("homeassistant")``. Once core no longer ships a ``HOMEASSISTANT``
enum member, that only resolves for a platform the registry knows, so register the plugin's real
``PlatformEntry`` up front exactly as the loader would.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PACKAGE = "homeassistant_plugin"
_ROOT = Path(__file__).resolve().parents[4] / "plugins" / "homeassistant"


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def _ensure_platform_registered(plugin) -> None:
    from gateway.platform_registry import PlatformEntry, platform_registry

    if platform_registry.is_registered(plugin.PLATFORM_NAME):
        return
    platform_registry.register(PlatformEntry(source="plugin", **plugin._platform_kwargs()))


_ensure_platform_registered(_load_plugin_package())
