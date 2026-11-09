"""``op_last_good.json`` holds secret values, so it sits at the top of the home beside ``op_cache.json``;
a copy in ``cache/`` (agent-writable, where older images kept it) is deleted without being read."""

import json

from agent.secret_sources import onepassword as op


def test_last_good_lives_at_home_root_and_a_planted_cache_copy_is_never_served(tmp_path):
    planted = tmp_path / "cache" / "op_last_good.json"
    planted.parent.mkdir()
    planted.write_text(json.dumps({"ident": {"op://V/I/f": "attacker-value"}}), encoding="utf-8")

    assert op._read_last_good(tmp_path, "ident") == {}
    assert not planted.exists()

    op._write_last_good(tmp_path, "ident", {"op://V/I/f": "real"})
    assert op._last_good_path(tmp_path) == tmp_path / "op_last_good.json"
    assert op._read_last_good(tmp_path, "ident") == {"op://V/I/f": "real"}
    assert (tmp_path / "op_last_good.json").stat().st_mode & 0o777 == 0o600
