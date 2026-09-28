#!/usr/bin/env python3
"""Focused sync diagnostics. Execution belongs on disposable CI runners only.

No full-suite fallback, ref mutation, image build/publication or service startup.
The manifest deliberately separates observed failure from causal attribution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

LANES = ("identity-and-contracts", "windows-paths", "upgrade-config", "sqlite-wal",
         "affected-python", "image-inventory")
CLASSES = ("baseline fork debt", "inherited upstream behavior", "merge-only regression",
           "fork-policy incompatibility", "infrastructure/runner failure", "unresolved")
TARGETS = {
    "windows-paths": ["tests/scripts/install/test_install_ps1_*.py",
                      "tests/hermes_cli/test_jobs_json_utf8_bom.py",
                      "tests/plugins/memory/test_bom_tolerant_config_reads.py",
                      "tests/tools/test_bot_relay_windows_paths.py",
                      "tests/scripts/desktop_update/test_desktop_update_windows_*.py"],
    "upgrade-config": ["tests/hermes_cli/test_config_migration*.py",
                       "tests/hermes_cli/test_sibling_config_migration.py",
                       "tests/hermes_cli/test_update_config_migration_on_current.py",
                       "tests/hermes_cli/test_config_unversioned_migration.py"],
    "sqlite-wal": ["tests/hermes_state/test_sqlite_wal_reset_gate.py",
                   "tests/hermes_state/test_wal_active_confirmed.py",
                   "tests/hermes_state/test_wal_checkpoint_strategy.py"],
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def exact_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("expected a full lowercase commit SHA")
    if git("rev-parse", "--verify", value + "^{commit}") != value:
        raise ValueError("commit identity mismatch")
    return value


def selected_lanes(value: str) -> list[str]:
    lanes: list[str] = [str(name) for name in LANES] if value == "all" else value.split(",")
    if not lanes or len(set(lanes)) != len(lanes) or set(lanes) - set(LANES):
        raise ValueError("unknown, duplicate or empty selector")
    return lanes


def provenance(candidate: str, baseline: str, upstream: str, merge: str) -> dict:
    for value in (candidate, baseline, upstream, merge):
        exact_sha(value)
    if git("rev-parse", "HEAD") != candidate:
        raise ValueError("checkout does not equal candidate")
    if git("show", "-s", "--format=%P", merge).split() != [baseline, upstream]:
        raise ValueError("sync merge must have baseline first, upstream second (exact parents)")
    spine = git("rev-list", "--first-parent", candidate).splitlines()
    if merge not in spine:
        raise ValueError("sync merge is not on candidate's first-parent history")
    # Do not use merge-base..HEAD: that attributes thousands of upstream commits
    # to the fork. Exclude upstream reachability and retain the fork's spine.
    fork = git("rev-list", "--first-parent", candidate, "--not", upstream).splitlines()
    tranche = git("rev-list", "--first-parent", baseline + ".." + candidate).splitlines()
    return {"baseline": baseline, "upstream": upstream, "sync_merge": merge,
            "fork_first_parent_count": len(fork), "fork_first_parent": fork[:200],
            "fork_first_parent_sha256": hashlib.sha256("\n".join(fork).encode()).hexdigest(),
            "tranche_count": len(tranche), "tranche": tranche[:200],
            "truncated": len(fork) > 200 or len(tranche) > 200,
            "meaning": "fork-side provenance, not an assertion of personal authorship"}


def test_files(patterns: list[str]) -> list[str]:
    files = set()
    for pattern in patterns:
        # Inputs are repository-relative test roots/globs, never shell fragments.
        if not pattern.startswith("tests/") or ".." in Path(pattern).parts or "\\" in pattern:
            raise ValueError("test selectors must be repository-relative tests/ paths")
        if pattern.startswith(("tests/docker", "tests/e2e")):
            raise ValueError("lifecycle/E2E suites belong to later full acceptance")
        matches = list(Path().glob(pattern))
        resolved = []
        for match in matches:
            resolved.extend(match.rglob("test_*.py") if match.is_dir() else [match])
        matches = [p for p in resolved if p.name.startswith("test_") and p.suffix == ".py"]
        if not matches:
            raise ValueError("empty test selector: " + pattern)
        for path in matches:
            relative = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
            if not relative.startswith("tests/") or relative.startswith(("tests/docker/", "tests/e2e/")):
                raise ValueError("resolved selector crosses the focused-test boundary")
            files.add(path.as_posix())
    if not files or len(files) > 500:
        raise ValueError("select 1..500 test files; split broader work into focused runs")
    return sorted(files)


class Report:
    def __init__(self, args):
        self.args = args
        self.directory = Path(args.output)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / (args.lane + ".json")
        self.data = {"schema": 1, "candidate": args.candidate, "run_id": args.run_id,
                     "run_attempt": args.run_attempt, "job": args.lane,
                     "checks": [], "causal_classes": CLASSES}
        if args.phase == "tests":
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if (self.data["candidate"], self.data["run_id"], self.data["run_attempt"]) != (
                    args.candidate, args.run_id, args.run_attempt):
                raise ValueError("cannot resume a foreign receipt")
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")

    def record(self, name, status, cause="unresolved", **evidence):
        self.data["checks"].append({"name": name, "status": status,
                                    "causal_class": cause if status != "passed" else None,
                                    **evidence})
        self.save()

    def command(self, name, command, *, cause="unresolved", timeout=600):
        log = self.directory / (self.args.lane + "-" + name + ".log")
        try:
            with log.open("wb") as stream:
                result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=timeout, check=False)
            code = result.returncode
            self.record(name, "passed" if code == 0 else "failed", cause,
                        exit_code=code, log=log.name, command=command)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.record(name, "failed", "infrastructure/runner failure",
                        error=type(exc).__name__, log=log.name, command=command)
        # The JSON never embeds logs. Bound the separate diagnostic payload too.
        if log.exists() and log.stat().st_size > 128 * 1024:
            with log.open("rb") as stream:
                stream.seek(-128 * 1024, 2)
                suffix = stream.read()
            log.write_bytes(b"[earlier output truncated]\n" + suffix)


def source_inventory() -> dict:
    """Deterministic source inputs, NOT a claim about a built image's contents."""
    names = git("ls-files", "Dockerfile", ".dockerignore", "docker", "pm/lock.json",
                "pyproject.toml", "uv.lock", "package-lock.json").splitlines()
    rows = [{"path": name, "sha256": hashlib.sha256(Path(name).read_bytes()).hexdigest()}
            for name in sorted(names)]
    return {"kind": "image-source-inputs", "files": rows,
            "sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            "built_image_inspected": False}


def lane(args):
    report = Report(args)
    if git("rev-parse", "HEAD") != args.candidate:
        report.record("identity", "failed", detail="checkout/candidate mismatch")
        return 1
    py = args.python
    if args.lane == "identity-and-contracts" and args.phase != "tests":
        report.command("lock", [py, "-m", "pm.build_env", "--source", ".", "--check-lock"])
        report.command("npm-lock", ["npm", "ci", "--dry-run", "--ignore-scripts", "--no-audit", "--no-fund"])
        report.command("workflow-syntax", [args.actionlint, "-shellcheck=", "-pyflakes="])
        report.command("runner-policy", [py, "scripts/ci/check_larger_runner_guards.py"],
                       cause="fork-policy incompatibility")
        report.command("compat-imports", [py, "scripts/check_compat_pointers.py"])
    if args.phase == "cheap":
        # Dependency installation may fail; preserve the lock/policy tranche first.
        return 0
    if args.lane == "identity-and-contracts":
        report.command("dependency-closure", ["uv", "pip", "check", "--python", py])
        # Collection imports real modules; migration tests additionally resolve
        # dynamically accessed symbols during their focused execution lane.
        patterns = TARGETS["upgrade-config"] + ["tests/ci/test_sync_preflight.py"]
        report.command("import-migration-contracts",
                       ["bash", "scripts/run_tests.sh", *test_files(patterns)])
    elif args.lane == "image-inventory":
        inventory = source_inventory()
        (report.directory / "image-source-inputs.json").write_text(
            json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
        report.record("source-inventory", "passed", digest=inventory["sha256"],
                      evidence="image-source-inputs.json", built_image_inspected=False)
        # Syntax smoke only: never execute init scripts, Docker or s6.
        scripts = git("ls-files", "docker/*.sh", "docker/**/*.sh").splitlines()
        for index, script in enumerate(scripts):
            report.command("shell-" + str(index), ["bash", "-n", script])
    else:
        patterns = json.loads(args.roots) if args.lane == "affected-python" else TARGETS[args.lane]
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError("roots must be a JSON array of repository-relative paths")
        files = test_files(patterns)
        if args.lane == "sqlite-wal":
            report.command("sqlite-runtime", [py, "-c", "import sqlite3, hermes_state_wal as w; "
                           "print(sqlite3.sqlite_version); assert not w.is_sqlite_wal_reset_vulnerable()"])
        report.command("focused-tests", ["bash", "scripts/run_tests.sh", *files], timeout=1200)
    # A killed job must not turn its partial receipt into a green tranche.
    report.data["complete"] = True
    report.save()
    # Diagnostics do not short-circuit siblings. The final aggregation is the gate.
    return 0


def aggregate(args):
    try:
        lanes = ["identity", *selected_lanes(args.selectors)]
    except ValueError:
        # Identity already rejected the selector; still emit an aggregate receipt.
        lanes = ["identity"]
    reports = []
    for name in lanes:
        path = Path(args.output) / (name + ".json")
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("oversized report")
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data["candidate"], data["run_id"], data["run_attempt"], data["job"]) != (
                    args.candidate, args.run_id, args.run_attempt, name):
                raise ValueError("report identity mismatch")
            if not data["checks"] or len(data["checks"]) > 200:
                raise ValueError("invalid check count")
            if not data.get("complete"):
                data["checks"].append({"name": "incomplete", "status": "failed",
                                       "causal_class": "infrastructure/runner failure"})
            for check in data["checks"]:
                if check["status"] not in ("passed", "failed"):
                    raise ValueError("invalid status")
                if check["status"] == "failed" and check["causal_class"] not in CLASSES:
                    raise ValueError("invalid causal class")
            reports.append(data)
        except (OSError, ValueError, KeyError, TypeError):
            reports.append({"job": name, "checks": [{"name": "receipt", "status": "failed",
                            "causal_class": "infrastructure/runner failure",
                            "detail": "missing, invalid or mismatched lane receipt; inspect job result"}]})
    job_results = json.loads(getattr(args, "job_results", "{}"))
    for job, receipt in job_results.items():
        if receipt.get("result") != "success":
            reports.append({"job": job, "checks": [{"name": "job-result", "status": "failed",
                            "causal_class": "infrastructure/runner failure",
                            "detail": "GitHub job result: " + str(receipt.get("result"))}]})
    failures = [{"job": r["job"], **c} for r in reports for c in r["checks"] if c["status"] != "passed"]
    manifest = {"schema": 1, "candidate": args.candidate, "run_id": args.run_id,
                "run_attempt": args.run_attempt, "causal_classes": CLASSES,
                "selected_lanes": lanes, "full_acceptance": "not run", "reports": reports,
                "failure_count": len(failures)}
    Path(args.output, "failure-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    lines = ["# Hermes Sync Preflight", "", f"Candidate: `{args.candidate}`",
             f"Run: {args.run_id}, attempt {args.run_attempt}", "",
             "Full acceptance: NOT RUN. Image inventory covers source inputs only.", "",
             "| Job | Check | Causal class |", "| --- | --- | --- |"]
    lines += [f"| {f['job']} | {f['name']} | {f['causal_class']} |" for f in failures]
    if not failures:
        lines.append("\nNo failures in the selected tranche; unselected lanes are not accepted.")
    Path(args.output, "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("identity", "lane", "aggregate"))
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--baseline", default="")
    parser.add_argument("--upstream", default="")
    parser.add_argument("--merge", default="")
    parser.add_argument("--dispatch-sha", default="")
    parser.add_argument("--selectors", default="all")
    parser.add_argument("--roots", default='["tests/ci"]')
    parser.add_argument("--lane", default="identity", choices=("identity", *LANES))
    parser.add_argument("--phase", choices=("all", "cheap", "tests"), default="all")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--actionlint", default="actionlint")
    parser.add_argument("--job-results", default="{}")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--output", default="preflight")
    args = parser.parse_args()
    if args.mode == "aggregate":
        return aggregate(args)
    if args.mode == "identity":
        report = Report(args)
        try:
            if args.dispatch_sha != args.candidate:
                raise ValueError("dispatch SHA must equal candidate; dispatch on its branch/ref")
            selected_lanes(args.selectors)
            evidence = provenance(args.candidate, args.baseline, args.upstream, args.merge)
            report.record("identity-and-provenance", "passed", evidence=evidence)
            report.data["complete"] = True
            report.save()
            return 0
        except (ValueError, subprocess.CalledProcessError) as exc:
            report.record("identity-and-provenance", "failed", detail=str(exc)[:500])
            report.data["complete"] = True
            report.save()
            return 1
    try:
        return lane(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Preserve earlier independent results if discovery itself failed.
        path = Path(args.output, args.lane + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["checks"].append({"name": "lane-incomplete", "status": "failed",
                               "causal_class": "unresolved", "detail": str(exc)[:500]})
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
