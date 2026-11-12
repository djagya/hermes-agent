"""browser_control: control calls from Hermes with the held lease; tokens and key never reach the model."""

import hashlib
import json
import tempfile
from pathlib import Path

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
    assert out["url"] == "https://b.example/h#x" and (held / "handoff-target").read_text(encoding="utf-8-sig") == "TAB1"
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


@pytest.fixture
def upload_ready(monkeypatch, held):
    sent = []

    class Resp:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tool, "urlopen", lambda req, timeout: (sent.append(req.full_url), Resp())[1])
    return sent


def test_upload_refuses_hermes_secrets_and_symlink_escapes(upload_ready, held):
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    secret = home / ".env"
    secret.write_text("KEY=1\n", encoding="utf-8")
    assert "error" in _call(action="upload", path=str(secret))
    (held / ".env").write_text("KEY=1\n", encoding="utf-8")
    assert "credential or hidden" in _call(action="upload", path=str(held / ".env"))["error"]
    outside = Path(tempfile.mkdtemp()) / "notes.txt"
    outside.write_text("private\n", encoding="utf-8")
    (held / "report.pdf").symlink_to(outside)  # an innocent name inside the workspace, pointing out of it
    assert "Uploads come only" in _call(action="upload", path=str(held / "report.pdf"))["error"]
    assert upload_ready == []


def test_upload_from_the_workspace_is_staged(upload_ready, held):
    (held / "cv.pdf").write_bytes(b"%PDF-1")
    out = _call(action="upload", path=str(held / "cv.pdf"))
    assert out["staged"] == "/staging/cv.pdf" and len(upload_ready) == 1


def test_cancel_refuses_a_ticket_this_task_was_not_issued(monkeypatch, held):
    posts = []
    monkeypatch.setattr(route, "post_json", lambda path, payload: (posts.append(payload), {"cancelled": "q"})[1])
    assert "not issued to this task" in _call(action="cancel", request_id="q-other")["error"]
    route._remember_ticket("t1", "q-mine")
    assert _call(action="cancel", request_id="q-mine")["cancelled"] == "q-mine"
    assert posts == [{"request_id": "q-mine", "task": "t1"}]


def test_research_names_the_no_identity_lease_when_two_are_held(held):
    # dlz: with research and shopping held, "pass identity" was impossible to satisfy for research.
    route._held_leases["t1|shopping|"] = {route.MANAGED_LEASE_ENV: "lease-2", route.MANAGED_TOKEN_ENV: "tok-2",
                                          route.MANAGED_SLOT_ENV: "shopping", "BU_NAME": "bu2"}
    lease, error = tool._lease("t1")
    assert lease is None and "(research)" not in error and "(research, shopping)" in error
    for name in ("research", "(research)", "Research"):
        lease, error = tool._lease("t1", identity=name)
        assert error is None and lease[route.MANAGED_LEASE_ENV] == "lease-1", name
    lease, _ = tool._lease("t1", identity="shopping")
    assert lease[route.MANAGED_LEASE_ENV] == "lease-2"


# --- release: withdraw an unopened handoff link (revoke_unused_handoff) -------------------------------

@pytest.fixture
def release_posts(monkeypatch, held):
    """Record /v1/release bodies; replies come from ``replies`` (default: released). Daemon retires are recorded."""
    box = type("Box", (), {})()
    box.posts, box.retired, box.replies = [], [], []

    def fake_post(control, path, payload):
        assert path == "/v1/release"
        box.posts.append(dict(payload))
        return box.replies.pop(0) if box.replies else {"released": "lease-1"}

    monkeypatch.setattr(route, "_post", fake_post)
    monkeypatch.setattr(route, "_retire_daemon", lambda name: box.retired.append(name))
    route._minted_handoffs.clear()
    yield box
    route._minted_handoffs.clear()


_PENDING_REVOCABLE = {"error": "handoff_pending", "reason": "handoff_pending", "deferred": True, "revocable": True}
_PENDING_OPENED = {"error": "handoff_pending", "reason": "handoff_pending", "deferred": True, "revocable": False}


def test_release_body_has_the_flag_only_when_set(release_posts):
    assert _call(action="release")["success"] is True
    route._held_leases["t1||research"] = {route.MANAGED_LEASE_ENV: "lease-1", route.MANAGED_TOKEN_ENV: "tok-1",
                                         route.MANAGED_SLOT_ENV: "research-1", "BU_NAME": "bu"}
    assert _call(action="release", revoke_unused_handoff=True)["success"] is True
    route._held_leases["t1||research"] = {route.MANAGED_LEASE_ENV: "lease-1", route.MANAGED_TOKEN_ENV: "tok-1",
                                         route.MANAGED_SLOT_ENV: "research-1", "BU_NAME": "bu"}
    # Only a real JSON true counts; a string from the model does not.
    assert _call(action="release", revoke_unused_handoff="true")["success"] is True
    assert release_posts.posts == [
        {"lease_id": "lease-1", "token": "tok-1"},
        {"lease_id": "lease-1", "token": "tok-1", "revoke_unused_handoff": True},
        {"lease_id": "lease-1", "token": "tok-1"},
    ]


def test_deferred_revocable_keeps_the_lease_entry_but_retires_the_daemon(release_posts):
    release_posts.replies.append(dict(_PENDING_REVOCABLE))
    out = _call(action="release")
    assert out["deferred"] is True and out["revocable"] is True
    assert "revoke_unused_handoff=true" in out["note"]
    # The daemon goes before the POST in every case; only the lease entry (id/token) comes back.
    assert release_posts.retired == ["bu"] and "t1||research" in route._held_leases
    assert route.held_leases_for_task("t1")  # Sera can still address the lease


def test_revoking_second_release_frees_the_slot_and_drops_the_minted_claim(release_posts):
    route.note_handoff("t1", "research-1", "lease-1", 600)
    route.note_handoff("t1", "research-2", "lease-9", 600)  # another lease's claim is left alone
    release_posts.replies.append(dict(_PENDING_REVOCABLE))
    assert _call(action="release")["revocable"] is True
    assert ("t1", "research-1", "lease-1") in route._minted_handoffs
    out = _call(action="release", revoke_unused_handoff=True)
    assert out == {**out, "success": True, "released": "research-1"} and "deferred" not in out
    assert release_posts.posts[-1]["revoke_unused_handoff"] is True
    assert "t1||research" not in route._held_leases and release_posts.retired == ["bu", "bu"]
    assert list(route._minted_handoffs) == [("t1", "research-2", "lease-9")]


def test_deferred_not_revocable_keeps_the_old_behaviour(release_posts):
    release_posts.replies.append(dict(_PENDING_OPENED))
    out = _call(action="release", revoke_unused_handoff=True)
    assert out["deferred"] is True and "revocable" not in out
    assert "revoke_unused_handoff" not in out["note"] and "do not call release again" in out["note"]
    assert "t1||research" not in route._held_leases and "bu" in release_posts.retired


def test_old_control_deferral_without_revocable_does_not_claim_the_link_was_opened(release_posts):
    release_posts.replies.append({"error": "handoff_pending", "reason": "handoff_pending", "deferred": True})
    out = _call(action="release")
    assert out["deferred"] is True and "revocable" not in out
    assert "opened" not in out["note"] and "still live" in out["note"] and "used or expires" in out["note"]
    assert "t1||research" not in route._held_leases and "bu" in release_posts.retired


def test_raising_post_retires_the_daemon_and_keeps_the_lease_for_a_retry(monkeypatch, release_posts):
    import http.client

    def boom(control, path, payload):
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(route, "_post", boom)
    out = _call(action="release")
    assert "error" in out and "IncompleteRead" in out["error"]
    assert release_posts.retired == ["bu"]
    assert "t1||research" in route._held_leases  # not silently lost: a retry (or atexit) can still release it
    # The same through the cleanup shape: nothing escapes, the daemon is retired.
    release_posts.retired.clear()
    assert route.release_lease({route.MANAGED_LEASE_ENV: "lease-1", route.MANAGED_TOKEN_ENV: "tok-1",
                                "BU_NAME": "bu"}) is not None
    assert release_posts.retired == ["bu"]


def test_task_end_after_an_ignored_revocable_deferral_leaks_no_daemon(release_posts):
    route.note_handoff("t1", "research-1", "lease-1", 600)
    release_posts.replies.append(dict(_PENDING_REVOCABLE))
    assert _call(action="release")["revocable"] is True
    assert release_posts.retired == ["bu"]  # the only retire: nothing is left to leak
    # Claim still open: task end keeps the entry (as for any open handoff), still no daemon to retire.
    assert route.release_task_leases(["t1"]) == 0
    assert release_posts.retired == ["bu"]
    # Claim expired: the entry no longer blocks task-end cleanup, which releases it without the flag.
    route._minted_handoffs.clear()
    release_posts.replies.append(dict(_PENDING_REVOCABLE))
    assert route.release_task_leases(["t1"]) == 1
    assert "t1||research" not in route._held_leases
    assert release_posts.retired == ["bu", "bu"]
    assert "revoke_unused_handoff" not in release_posts.posts[-1]


def test_cleanup_shaped_release_retires_on_a_revocable_reply_and_restores_nothing(release_posts):
    release_posts.replies.append(dict(_PENDING_REVOCABLE))
    assert route.release_task_leases(["t1"]) == 1
    assert release_posts.retired == ["bu"] and not route._held_leases
    route._held_leases["t1||research"] = {route.MANAGED_LEASE_ENV: "lease-1", route.MANAGED_TOKEN_ENV: "tok-1",
                                         route.MANAGED_SLOT_ENV: "research-1", "BU_NAME": "bu"}
    release_posts.replies.append(dict(_PENDING_REVOCABLE))
    route.release_all_held_leases()
    assert release_posts.retired == ["bu", "bu"] and not route._held_leases


def test_restore_does_not_clobber_a_concurrent_reacquire(monkeypatch, release_posts):
    fresh = {route.MANAGED_LEASE_ENV: "lease-2", route.MANAGED_TOKEN_ENV: "tok-2",
             route.MANAGED_SLOT_ENV: "research-1", "BU_NAME": "bu2"}

    def racing_post(control, path, payload):
        release_posts.posts.append(dict(payload))
        route._held_leases["t1||research"] = dict(fresh)  # a parallel browser_exec acquired meanwhile
        return dict(_PENDING_REVOCABLE)

    monkeypatch.setattr(route, "_post", racing_post)
    assert _call(action="release")["revocable"] is True
    assert route._held_leases["t1||research"] == fresh
    assert release_posts.retired == ["bu"]  # the old lease's daemon was retired before the POST


def test_cleanup_paths_never_send_the_flag(release_posts):
    route._held_leases["t2||research"] = {route.MANAGED_LEASE_ENV: "lease-2", route.MANAGED_TOKEN_ENV: "tok-2",
                                         route.MANAGED_SLOT_ENV: "research-2", "BU_NAME": "bu2"}
    assert route.release_task_leases(["t2"]) == 1
    route.release_all_held_leases()
    assert [p["lease_id"] for p in release_posts.posts] == ["lease-2", "lease-1"]
    assert all("revoke_unused_handoff" not in p for p in release_posts.posts)


def test_schema_lists_the_flag():
    prop = tool.BROWSER_CONTROL_SCHEMA["parameters"]["properties"]["revoke_unused_handoff"]
    assert prop["type"] == "boolean" and "handoff link" in prop["description"]
