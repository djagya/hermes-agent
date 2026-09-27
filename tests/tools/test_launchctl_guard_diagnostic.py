"""Diagnostics must distinguish policy rejection from inspected job properties."""

import json
import plistlib

from tools.terminal_tool_guards import gateway_lifecycle_block

LIFECYCLE = "hermes gateway restart"


def _supervised(monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)


def test_bootstrap_rejection_does_not_invent_keepalive(tmp_path, monkeypatch):
    _supervised(monkeypatch)
    plist = tmp_path / "com.example.schedule.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.example.schedule",
        "ProgramArguments": ["/bin/true"],
        "RunAtLoad": False,
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
    }))
    blocked = gateway_lifecycle_block(
        command=f"launchctl bootstrap gui/501 {plist}",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is not None
    result = json.loads(blocked)
    assert result["exit_code"] == 1
    assert "regardless of the job label" in result["error"]
    assert "does not inspect" in result["error"]
    assert "KeepAlive settings" in result["error"]
    assert "separate shell outside the gateway" in result["error"]


def test_observed_lifecycle_block_keeps_its_meaning(tmp_path, monkeypatch):
    """A lifecycle command OBSERVED in a referenced script keeps the original
    refusal text: it really did see a gateway-restart operation."""
    _supervised(monkeypatch)
    script = tmp_path / "restart.sh"
    script.write_text(f"#!/bin/sh\n{LIFECYCLE}\n", encoding="utf-8")
    blocked = gateway_lifecycle_block(
        command=f"bash {script}",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is not None
    result = json.loads(blocked)
    assert result["exit_code"] == 1
    assert "cannot restart, stop, or uninstall the gateway" in result["error"]
    assert "separate shell outside the running gateway" in result["error"]


def test_inconclusive_oversize_reference_does_not_imply_restart(tmp_path, monkeypatch):
    """The terminal sibling consumes typed verdicts: an oversized referenced file
    is refused WITHOUT the restart-implying message (cron-path parity)."""
    _supervised(monkeypatch)
    import cron.lifecycle_guard as lifecycle_guard

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"echo ok\n" + b"x" * (lifecycle_guard._MAX_REFERENCED_SCRIPT_BYTES + 100))
    blocked = gateway_lifecycle_block(
        command=f"bash {huge}",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is not None
    result = json.loads(blocked)
    assert result["exit_code"] == 1
    assert "could not scan" in result["error"]
    assert "cannot restart, stop, or uninstall" not in result["error"]


def test_clean_command_passes(tmp_path, monkeypatch):
    _supervised(monkeypatch)
    blocked = gateway_lifecycle_block(
        command="echo hello",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is None


def test_interpreter_kill_rejection_names_the_owned_process_route(tmp_path, monkeypatch):
    """Scheduled-Task topology (#113667): only ``HERMES_SUPERVISED_CHILD`` marks the launch, and an
    image-name kill of the interpreter is refused with the ``proc_*`` / explicit-PID route named.
    Other images and explicit PIDs pass."""
    import os

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    for marker in ("INVOCATION_ID", "XPC_SERVICE_NAME", "HERMES_S6_SUPERVISED_CHILD", "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: os.getpid())

    def run(command):
        return gateway_lifecycle_block(
            command=command, env=None, env_type="local", cwd=str(tmp_path), workdir=None,
            session_key="diagnostic-test",
        )

    blocked = json.loads(run("taskkill /F /IM python.exe 2>/dev/null | head -2"))
    assert blocked["exit_code"] == 1
    assert "proc_" in blocked["error"] and "explicit PID" in blocked["error"]
    assert "proc_" in json.loads(run("pkill -9 python3"))["error"]
    assert run("taskkill /F /IM agent-browser.exe /T") is None
    assert run("taskkill /F /PID 46544") is None
    # Absence on the wrong side: a plain foreground `hermes gateway run` carries no launch marker.
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD")
    assert run("pkill -9 python3") is None
