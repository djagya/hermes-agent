"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "pr_state": "OPEN",
             "pull_state": "open", "merged": False, "graphql_errors": None,
             "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                if state.get("graphql_errors"):
                    value = {"data": None, "errors": state["graphql_errors"]}
                else:
                    value = {"data": {"repository": {"pullRequest": {
                        "headRefOid": sha, "baseRefName": "main", "state": state["pr_state"],
                        "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                            {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": state["pull_state"]}
                if state["merged"]:
                    value["merged"] = True
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.linux_only
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


def _last_receipt(conn, tid):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance' ORDER BY id DESC", (tid,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.linux_only
def test_required_failures_refuse_with_typed_evidence(github):
    """Failed/pending/missing required checks refuse, each with bound head and per-check classification."""
    with connect() as conn:
        for conclusion, expected in (("failure", "failure"), ("pending", "pending")):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title=conclusion, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            receipt = _last_receipt(conn, tid)
            assert receipt["ok"] is False and receipt["classification"] == expected
            assert receipt["head_sha"] == "a" * 40
            assert receipt["checks"] and receipt["checks"][0]["classification"] == expected
            assert kb.get_task(conn, tid).status != "done"
        github.update(conclusion="success", head="a" * 40, missing=True)
        tid = kb.create_task(conn, title="missing", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = _last_receipt(conn, tid)
        assert receipt["classification"] == "missing"
        assert receipt["head_sha"] == "a" * 40
        assert receipt["checks"] == [{"name": "required", "classification": "missing", "head_sha": "a" * 40}]


@pytest.mark.linux_only
def test_merged_pr_with_provable_exact_head_and_required_checks_is_acceptable(github):
    """Merged PRs stay completable when exact head + required checks prove out (t_06d13cf0 shape)."""
    github.update(pr_state="MERGED", pull_state="closed", merged=True, head="a" * 40, conclusion="success")
    with connect() as conn:
        tid = kb.create_task(conn, title="merged", completion_contract="djagya/hermes-agent")
        assert kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/djagya/hermes-agent/pull/18"})
        assert kb.get_task(conn, tid).status == "done"
        receipt = _last_receipt(conn, tid)
        assert receipt["ok"] is True and receipt["classification"] == "success"
        assert receipt["head_sha"] == "a" * 40
        assert receipt["checks"][0]["name"] == "required"
        assert receipt["checks"][0]["head_sha"] == "a" * 40


@pytest.mark.linux_only
def test_missing_gh_binary_is_a_typed_capability_result(github, tmp_path, monkeypatch):
    """No gh CLI => explicit capability classification, never the generic null-head infra ambiguity."""
    import hermes_cli.kanban_pr_acceptance as acc
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "capability"
    assert receipt["head_sha"] is None and receipt["checks"] == []
    assert "gh CLI" in receipt["detail"]
    assert receipt["pr_url"] == "https://github.com/acme/repo/pull/7"


@pytest.mark.linux_only
def test_unauthenticated_gh_is_typed_auth_unavailable(github, tmp_path, monkeypatch):
    """gh's exit-4 login banner maps to auth_unavailable, not null-head infra."""
    import hermes_cli.kanban_pr_acceptance as acc
    shim = tmp_path / "nologin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text("#!/bin/sh\n"
                  "echo 'To get started with GitHub CLI, please run: gh auth login' >&2\n"
                  "echo 'Alternatively, populate the GH_TOKEN environment variable.' >&2\n"
                  "exit 4\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "auth_unavailable"
    assert receipt["head_sha"] is None
    assert "authenticated" in receipt["detail"]


@pytest.mark.linux_only
def test_graphql_bad_credentials_map_to_auth_unavailable(github):
    """GraphQL errors[] bodies carrying Bad credentials become typed auth failures."""
    import hermes_cli.kanban_pr_acceptance as acc
    github["graphql_errors"] = [{"message": "Bad credentials"}]
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "auth_unavailable"
    assert receipt["head_sha"] is None


@pytest.mark.linux_only
def test_failure_receipts_never_embed_gh_stderr_or_token_material(github, monkeypatch):
    """No gh stderr, token shape, or env echo survives into any receipt classification."""
    import hermes_cli.kanban_pr_acceptance as acc
    token = "ghp_" + "q" * 36

    def bad_credentials(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=command, output="",
            stderr=f"gh: Bad credentials for GH_TOKEN={token} (HTTP 401)")

    monkeypatch.setattr(acc.subprocess, "run", bad_credentials)
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    blob = json.dumps(receipt)
    assert receipt["classification"] == "auth_unavailable"
    # The token VALUE and raw gh stderr never survive; naming the env var is fine.
    assert token not in blob and "gh: " not in blob and "Bad credentials for" not in blob

    # Bare legacy 40-hex credentials carry no denylist prefix, so no stderr
    # substring may reach the receipt at all (regression: t_488ac3cc finding).
    legacy = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

    def legacy_credential(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=command, output="",
            stderr=f"gh: credential {legacy} rejected for api.github.com")

    monkeypatch.setattr(acc.subprocess, "run", legacy_credential)
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "provider_error"
    assert receipt["detail"] == "GitHub API call failed (rc=1)."
    assert legacy not in receipt["detail"] and "credential" not in receipt["detail"]
    blob = json.dumps(receipt)
    assert legacy not in blob and "api.github.com" not in blob and "gh: " not in blob

    def ugly_error(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=command, output="",
            stderr=f"gh: token {token} expired; Authorization: token gho_{'s' * 36} for api.github.com")

    monkeypatch.setattr(acc.subprocess, "run", ugly_error)
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    blob = json.dumps(receipt)
    assert "ghp_" not in blob and "gho_" not in blob and "api.github.com" not in blob
    assert receipt["classification"] == "provider_error"


@pytest.mark.linux_only
def test_historical_infra_shape_is_now_typed(github, tmp_path, monkeypatch):
    """The exact t_06d13cf0/t_5346b4d6 receipt (infra + null head + empty checks) becomes typed."""
    import hermes_cli.kanban_pr_acceptance as acc
    real_path = os.environ["PATH"]
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    receipt = acc.collect_acceptance(
        "djagya/hermes-agent", "https://github.com/djagya/hermes-agent/pull/18")
    assert receipt["classification"] in {"capability", "auth_unavailable"}
    assert receipt["classification"] != "infra"  # the ambiguity is gone
    assert receipt["pr_url"] == "https://github.com/djagya/hermes-agent/pull/18"
    assert receipt["head_sha"] is None and receipt["checks"] == []
    assert receipt["detail"] != ("GitHub acceptance evidence unavailable or incomplete; "
                                 "check gh authentication/API access and retry.")
    # Same contract through the fixture's authenticated shim still reaches real evidence.
    monkeypatch.setenv("PATH", real_path)
    receipt = acc.collect_acceptance(
        "djagya/hermes-agent", "https://github.com/djagya/hermes-agent/pull/19")
    assert receipt["classification"] == "success"
    assert receipt["head_sha"] == "a" * 40
    assert receipt["pr_url"] == "https://github.com/djagya/hermes-agent/pull/19"


@pytest.mark.linux_only
def test_unscoped_multiplex_secret_read_refuses_as_capability(github, monkeypatch):
    """Multiplex mode with no secret scope fails closed as capability; os.environ is never read."""
    import agent.secret_scope as scope
    import hermes_cli.kanban_pr_acceptance as acc
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "u" * 36 + "unscoped_env_must_not_be_read")
    monkeypatch.setattr(scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setattr(scope, "_SECRET_SCOPE", scope.ContextVar("_SECRET_SCOPE", default=None))
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "capability"
    assert "secret scope" in receipt["detail"]
    assert "unscoped_env_must_not_be_read" not in json.dumps(receipt)
