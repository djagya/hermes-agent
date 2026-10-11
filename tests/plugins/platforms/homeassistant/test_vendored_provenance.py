"""dlz: the bundled homeassistant plugin is the catalog pin plus only the fork patches it records."""

import hashlib
import json
from pathlib import Path

import hermes_yaml

REPO = Path(__file__).resolve().parents[4]
PLUGIN = REPO / "plugins" / "homeassistant"
VENDORED = json.loads((PLUGIN / "VENDORED.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pin_matches_the_catalog_entry():
    entry = hermes_yaml.safe_load((REPO / VENDORED["catalog_entry"]).read_text(encoding="utf-8"))
    assert entry["sha"] == VENDORED["commit"]
    assert str(entry["version"]) == VENDORED["version"]
    manifest = hermes_yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    assert str(manifest["version"]) == VENDORED["version"] and manifest["kind"] == "platform"


def _check(files: dict, root: Path) -> None:
    for name, meta in files.items():
        actual = _sha256(root / name)
        if "fork_patch" in meta:
            # A patched file must differ from upstream; its fork state is pinned too when recorded.
            assert actual != meta["upstream_sha256"], f"{name}: recorded as patched but is pristine"
            if "fork_sha256" in meta:
                assert actual == meta["fork_sha256"], f"{name}: fork patch changed without VENDORED.json"
        else:
            assert actual == meta["upstream_sha256"], f"{name}: drifted from the pinned upstream copy"


def test_plugin_files_are_the_pinned_upstream_or_recorded_fork_patches():
    _check(VENDORED["files"], PLUGIN)
    shipped = {p.name for p in PLUGIN.iterdir() if p.is_file() and p.name != "VENDORED.json"}
    assert shipped == set(VENDORED["files"]), "an unrecorded file is shipped in the bundled plugin"


def test_vendored_tests_are_recorded():
    _check(VENDORED["tests"], Path(__file__).resolve().parent)
