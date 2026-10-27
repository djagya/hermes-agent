"""dlz: ``/memory pending`` then ``approve`` on a staged write through ``command.dispatch``.

The fork's variant of upstream's test of the same name (#118812) in test_tui_gateway_server.py,
kept in its own file so upstream's 23k-line module carries no fork lines.
"""

import threading
import types

from tools import write_approval as wa
from tui_gateway import server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


def test_command_dispatch_memory_pending_reviews_staged_writes(tmp_path, monkeypatch):
    # Same fallback for /memory (#118812): the store must resolve under the session's
    # profile home, applying the write on approve instead of dropping it.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  write_approval: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": "dispatched fact"},
                   summary="add dispatched fact", origin="foreground")
    server._sessions["sid-wa"] = _session(profile_home=str(home))
    try:
        res = server._methods["command.dispatch"](
            "d1", {"name": "memory", "arg": "pending", "session_id": "sid-wa"})
        assert res["result"]["type"] == "exec", res
        assert "dispatched fact" in res["result"]["output"]

        approve = server._methods["command.dispatch"](
            "d2", {"name": "memory", "arg": "approve all", "session_id": "sid-wa"})
        assert "Approved 1" in approve["result"]["output"], approve
        assert wa.pending_count(wa.MEMORY) == 0
    finally:
        server._sessions.pop("sid-wa", None)
