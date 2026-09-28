"""Behavior contracts for fail-closed sync receipts and fork-side provenance."""
import json
import subprocess
from argparse import Namespace

import pytest

from scripts.ci import sync_preflight as preflight


def test_provenance_excludes_upstream_history_and_requires_exact_parents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", *args], text=True,
        ).strip()

    git("init", "-b", "fork")
    git("commit", "--allow-empty", "-m", "common")
    common = git("rev-parse", "HEAD")
    git("switch", "-c", "upstream")
    git("commit", "--allow-empty", "-m", "upstream work")
    upstream = git("rev-parse", "HEAD")
    git("switch", "fork")
    git("commit", "--allow-empty", "-m", "fork work")
    baseline = git("rev-parse", "HEAD")
    git("merge", "--no-ff", "upstream", "-m", "sync")
    merge = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "candidate correction")
    candidate = git("rev-parse", "HEAD")

    result = preflight.provenance(candidate, baseline, upstream, merge)
    assert result["fork_first_parent"] == [candidate, merge, baseline]
    assert upstream not in result["fork_first_parent"]
    assert common not in result["fork_first_parent"]
    assert result["tranche"] == [candidate, merge]
    with pytest.raises(ValueError, match="exact parents"):
        preflight.provenance(candidate, upstream, baseline, merge)
    with pytest.raises(ValueError, match="checkout"):
        preflight.provenance(merge, baseline, upstream, merge)
    with pytest.raises(ValueError, match="full lowercase"):
        preflight.provenance(candidate[:12], baseline, upstream, merge)


@pytest.mark.parametrize("damage", [None, "missing", "partial", "wrong-head", "wrong-attempt", "failed", "job-failed"])
def test_aggregation_never_accepts_missing_partial_or_foreign_evidence(tmp_path, damage):
    args = Namespace(candidate="a" * 40, run_id="123", run_attempt="1",
                     selectors="sqlite-wal", output=str(tmp_path),
                     job_results=json.dumps({"focused": {"result": "failure"}})
                     if damage == "job-failed" else "{}")
    for name in ("identity", "sqlite-wal"):
        data = {"schema": 1, "job": name, "candidate": args.candidate,
                "run_id": args.run_id, "run_attempt": args.run_attempt, "complete": True,
                "checks": [{"name": "contract", "status": "passed", "causal_class": None}]}
        if name == "sqlite-wal":
            if damage == "missing":
                continue
            if damage == "partial":
                data["complete"] = False
            if damage == "wrong-head":
                data["candidate"] = "b" * 40
            if damage == "wrong-attempt":
                data["run_attempt"] = "2"
            if damage == "failed":
                data["checks"][0].update(status="failed", causal_class="unresolved")
        (tmp_path / (name + ".json")).write_text(json.dumps(data), encoding="utf-8")
    code = preflight.aggregate(args)
    manifest = json.loads((tmp_path / "failure-manifest.json").read_text(encoding="utf-8"))
    assert bool(code) == (damage is not None)
    assert bool(manifest["failure_count"]) == (damage is not None)
    assert manifest["candidate"] == args.candidate
    assert manifest["full_acceptance"] == "not run"
    assert "Full acceptance: NOT RUN" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_parent_blobs_and_mixed_causal_labels_survive_failed_manifest(tmp_path, monkeypatch):
    refs = {"baseline": "a" * 40, "upstream": "b" * 40,
            "merge": "c" * 40, "candidate": "d" * 40}
    args = Namespace(**refs, run_id="123", run_attempt="1", selectors="identity-and-contracts",
                     output=str(tmp_path), job_results="{}")
    blobs = {"baseline": "1" * 40, "upstream": "2" * 40,
             "sync_merge": "3" * 40, "candidate": "4" * 40}
    def fake_git(*argv):
        ref = argv[-1].split(":", 1)[0]
        path = argv[-1].split(":", 1)[1]
        key = "sync_merge" if ref == args.merge else next(k for k, v in refs.items() if v == ref)
        if argv[0] == "show":
            return "faulty-fragment" if (path, key) in {
                ("ci.yaml", "baseline"), ("ci.yaml", "sync_merge"),
                ("bundle.yml", "upstream"), ("bundle.yml", "sync_merge"),
                ("sdk.yml", "candidate"),
            } else ""
        blob = blobs[key]
        if (path == "ci.yaml" and key == "sync_merge" or
                path == "bundle.yml" and key == "sync_merge" or
                path == "sdk.yml" and key == "sync_merge"):
            blob = blobs[{"ci.yaml": "baseline", "bundle.yml": "upstream", "sdk.yml": "upstream"}[path]]
        return blob if argv[0] == "rev-parse" else ""
    monkeypatch.setattr(preflight, "git", fake_git)
    evidence = {
        "ci.yaml": preflight.parent_evidence(args, "ci.yaml", "baseline fork debt", "faulty-fragment"),
        "bundle.yml": preflight.parent_evidence(args, "bundle.yml", "inherited upstream behavior", "faulty-fragment"),
        "sdk.yml": preflight.parent_evidence(args, "sdk.yml", "fork-policy incompatibility", "faulty-fragment"),
    }
    assert preflight.parent_evidence(args, "bundle.yml", "baseline fork debt", "faulty-fragment")[
        "causal_class"] == "unresolved"
    for name in ("identity", "identity-and-contracts"):
        checks = [{"name": "identity", "status": "passed", "causal_class": None}]
        if name == "identity-and-contracts":
            checks = [{"name": "workflow-syntax", "status": "failed", "causal_class": "unresolved",
                       "causal_evidence": evidence}]
        (tmp_path / (name + ".json")).write_text(json.dumps({
            "job": name, "candidate": args.candidate, "run_id": args.run_id,
            "run_attempt": args.run_attempt, "complete": True, "checks": checks,
        }), encoding="utf-8")
    assert preflight.aggregate(args) == 1
    manifest = json.loads((tmp_path / "failure-manifest.json").read_text(encoding="utf-8"))
    stored = manifest["reports"][1]["checks"][0]["causal_evidence"]
    assert {v["causal_class"] for v in stored.values()} == {
        "baseline fork debt", "inherited upstream behavior", "fork-policy incompatibility"}
    assert stored["ci.yaml"]["blobs"]["baseline"] == stored["ci.yaml"]["blobs"]["sync_merge"]
    assert stored["bundle.yml"]["blobs"]["upstream"] == stored["bundle.yml"]["blobs"]["sync_merge"]
    assert stored["sdk.yml"]["blobs"]["upstream"] == stored["sdk.yml"]["blobs"]["sync_merge"]


def test_syntax_dispatch_uses_shebang_not_suffix(tmp_path):
    python_script = tmp_path / "guard.sh"
    python_script.write_text("#!/usr/bin/env python3\nprint('fixture')\n", encoding="utf-8")
    shell_script = tmp_path / "init.sh"
    shell_script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    python_command = preflight.syntax_command(str(python_script), "locked-python")
    assert python_command is not None
    assert python_command[0] == "locked-python"
    assert python_command[-1] == str(python_script)
    assert preflight.syntax_command(str(shell_script), "locked-python") == ["bash", "-n", str(shell_script)]
    assert preflight.focused_test_command("windows-paths", "locked-python", ["tests/x.py"], "win32") == [
        "locked-python", "scripts/run_tests_parallel.py", "tests/x.py"]
