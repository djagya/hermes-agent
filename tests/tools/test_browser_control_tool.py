"""browser_control: control calls from Hermes with the held lease; tokens and key never reach the model."""

import hashlib
import json

import pytest

from tools import browser_control_route as route, browser_control_tool as tool


@pytest.fixture
def held(monkeypatch, tmp_path):
    route.clear_held_leases()
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_URL", "http://control.invalid")
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_KEY", "secret-key")
    route._held_leases["t1||research"] = {route.MANAGED_LEASE_ENV: "lease-1", route.MANAGED_TOKEN_ENV: "tok-1",
                                         route.MANAGED_SLOT_ENV: "research-1", "BU_NAME": "bu"}
    monkeypatch.setattr("tools.browser_use_cli._workspace_dir", lambda task_id: str(tmp_path))
    yield tmp_path
    route.clear_held_leases()


def _call(**args):
    return json.loads(tool.browser_control(args, task_id="t1"))


def test_handoff_uses_the_held_lease_saves_the_target_and_leaks_no_token(monkeypatch, held):
    posts = []
    monkeypatch.setattr(route, "post_json", lambda path, payload: (posts.append((path, payload)),
                                                                   {"url": "https://b.example/h#x", "expires_in": 1200})[1])
    out = _call(action="handoff", target_id="TAB1")
    assert posts == [("/v1/handoff", {"lease_id": "lease-1", "command_token": "tok-1", "target_id": "TAB1"})]
    assert out["url"] == "https://b.example/h#x" and (held / "handoff-target").read_text() == "TAB1"
    assert "tok-1" not in json.dumps(out) and "secret-key" not in json.dumps(out)


def test_handoff_without_target_is_refused(held):
    assert "target_id is required" in _call(action="handoff")["error"]


def test_download_refuses_bytes_that_do_not_match_control_sha(monkeypatch, held):
    class Resp:
        headers = {"X-Sha256": hashlib.sha256(b"other").hexdigest()}

        def read(self):
            return b"payload"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tool, "urlopen", lambda req, timeout: Resp())
    out = _call(action="download", name="report.pdf")
    assert "do not match" in out["error"] and not (held / "downloads" / "report.pdf").exists()


def test_status_hides_lease_ids(monkeypatch, held):
    monkeypatch.setattr(route, "get_json", lambda path: {"slots": {"research-1": {
        "slot_id": "research-1", "state": "agent", "lease_id": "lease-1", "task": "t1"}}, "queue": []})
    out = _call(action="status")
    assert out["slots"] == [{"slot_id": "research-1", "state": "agent", "mine": True}]
    assert "lease-1" not in json.dumps(out)
