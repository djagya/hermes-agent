"""dlz: the image ships the homeassistant catalog plugin bundled (plugins/homeassistant/VENDORED.json).

HERMES_DISABLE_LAZY_INSTALLS=1 keeps the left-core migration from fetching it at gateway start, so
the sealed runtime must discover it from /opt/hermes/plugins with its ha_* tools and treat it as
present for the migration."""
from __future__ import annotations

import json
import subprocess

_PROBE = r'''
import json, tempfile, os
os.environ["HERMES_HOME"] = tempfile.mkdtemp()
from hermes_cli.plugins import PluginManager
from hermes_cli.left_core_migration import plugin_present
from pathlib import Path
from toolsets import resolve_toolset
mgr = PluginManager(); mgr.discover_and_load()
ha = mgr._plugins.get("homeassistant")
print(json.dumps({"found": ha is not None, "source": ha and ha.manifest.source,
                  "tools": sorted(resolve_toolset("homeassistant")),
                  "present": plugin_present("homeassistant", Path(os.environ["HERMES_HOME"]))}))
'''


def test_image_bundles_the_homeassistant_plugin(built_image: str) -> None:
    r = subprocess.run(
        ["docker", "run", "--rm", "--network=none", "--user", "hermes", "--entrypoint",
         "/opt/hermes/.venv/bin/python", "-w", "/opt/hermes", built_image, "-c", _PROBE],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"found": True, "source": "bundled", "present": True,
                   "tools": ["ha_call_service", "ha_get_state", "ha_list_entities", "ha_list_services"]}
