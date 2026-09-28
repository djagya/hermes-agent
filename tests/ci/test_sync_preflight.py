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
