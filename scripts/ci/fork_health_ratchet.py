"""Code-health ratchet for a PR that imports an upstream release (fork CI, djagya/hermes-agent).

The stock job (``scripts/check --only health --base HEAD^1``) judges the PR merge commit
against the base branch tip. A release-tracking PR into fork ``main`` also carries the upstream
sync merge, so every line upstream grew since the last tracked release reads as new debt: 481
blocking findings on the v0.21.6 tracking PR, none of them written in the fork.

A sync merge is a first-parent merge whose second parent is a commit upstream tags as a
release. Tags are read live from the upstream repository, so a tag made on the fork cannot
qualify. When the PR has one, its first-parent line splits into segments:

* blocking: fork commits on top of the newest sync merge (that merge -> PR head). This is code
  written against the imported upstream; a fix commit on the branch can still clear it.
* advisory: older fork segments. They landed on an earlier release before this import, and
  only rewriting the release line could change them.
* not measured: the sync merges (upstream's code plus conflict resolutions; sync-preflight
  covers those) and merges of the base branch (already judged when they landed there).

A PR without a sync merge runs the stock command unchanged. Every segment reads the
ENFORCEMENT switch from the base branch tip, never from the branch under test.

    python -m scripts.ci.fork_health_ratchet --base HEAD^1 --head HEAD^2
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"
STOCK = ("scripts/check", "--only", "health", "--base", "HEAD^1")


@dataclass(frozen=True)
class Segment:
    base: str
    head: str
    blocking: bool


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=300, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_tag_commits(ls_remote: str) -> frozenset[str]:
    """Commits that ``git ls-remote --tags`` output points at; an annotated tag counts as its
    peeled commit (the ``^{}`` line), never as the tag object."""
    by_ref: dict[str, str] = {}
    for line in ls_remote.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.endswith("^{}"):
            by_ref[ref[:-3]] = sha
        elif ref:
            by_ref.setdefault(ref, sha)
    return frozenset(by_ref.values())


def upstream_tag_commits(url: str) -> frozenset[str]:
    proc = subprocess.run(["git", "ls-remote", "--tags", url], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-remote --tags {url} failed: {proc.stderr.strip()}")
    return parse_tag_commits(proc.stdout)


def first_parent_line(repo: Path, base: str, head: str) -> list[tuple[str, list[str]]]:
    """``(commit, parents)`` on ``head``'s first-parent line not reachable from ``base``,
    newest first."""
    out = _git(repo, "rev-list", "--first-parent", "--parents", head, f"^{base}")
    return [(parts[0], parts[1:]) for parts in (line.split() for line in out.splitlines()) if parts]


def plan(line: list[tuple[str, list[str]]], is_upstream: Callable[[str], bool],
         on_base: Callable[[str], bool]) -> list[Segment] | None:
    """Segments to judge, oldest first; None when no merge on the line imports an upstream
    release (the caller runs the stock check)."""
    if not any(len(parents) > 1 and is_upstream(parents[1]) for _, parents in line):
        return None
    segments: list[Segment] = []
    newest = run_base = None
    after_sync = True  # walking newest -> oldest: still above the newest sync merge
    for commit, parents in line:
        sync = len(parents) > 1 and is_upstream(parents[1])
        if sync or (len(parents) > 1 and on_base(parents[1])):
            if newest is not None:
                segments.append(Segment(run_base, newest, after_sync))
            newest = run_base = None
            after_sync = after_sync and not sync
            continue
        newest = newest or commit
        run_base = parents[0]  # the oldest commit so far: its first parent is the run's base
    if newest is not None:
        segments.append(Segment(run_base, newest, after_sync))
    return segments[::-1]


def _judge(repo: Path, segment: Segment, switch_rev: str) -> int:
    from scripts.code_health import cli, gitio

    kind = "blocking" if segment.blocking else "advisory (landed before this upstream import)"
    print(f"\n=== code health segment {segment.base[:12]}..{segment.head[:12]}: {kind} ===", flush=True)
    try:
        rc = cli.run(repo, segment.base, gitio.resolve_tree(repo, segment.head), switch_rev=switch_rev)
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"code health: {exc}", file=sys.stderr)
        return 2
    return rc if segment.blocking else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fork_health_ratchet", description=__doc__.split("\n\n")[0])
    p.add_argument("--base", default="HEAD^1", help="base branch tip (the PR merge's first parent)")
    p.add_argument("--head", default="HEAD^2", help="PR head (the PR merge's second parent)")
    p.add_argument("--upstream", default=os.environ.get("HERMES_UPSTREAM_URL", UPSTREAM_URL))
    args = p.parse_args(argv)
    repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())

    def stock() -> int:
        return subprocess.run([sys.executable, *STOCK], cwd=repo, timeout=1800, check=False).returncode

    try:
        base = _git(repo, "rev-parse", "--verify", f"{args.base}^{{commit}}").strip()
        head = _git(repo, "rev-parse", "--verify", f"{args.head}^{{commit}}").strip()
    except RuntimeError:
        print("code health: no PR merge parents (not a pull_request merge); stock check")
        return stock()
    line = first_parent_line(repo, base, head)
    if not any(len(parents) > 1 for _, parents in line):
        return stock()
    tagged = upstream_tag_commits(args.upstream)

    def on_base(commit: str) -> bool:
        return subprocess.run(["git", "merge-base", "--is-ancestor", commit, base], cwd=repo,
                              timeout=120, check=False).returncode == 0

    segments = plan(line, tagged.__contains__, on_base)
    if segments is None:
        return stock()
    syncs = [c[:12] for c, parents in line if len(parents) > 1 and parents[1] in tagged]
    print(f"code health: PR imports upstream release(s) via sync merge {', '.join(syncs)}; "
          f"judging {len(segments)} fork segment(s), upstream's own changes excluded")
    worst = 0
    for segment in segments:
        worst = max(worst, _judge(repo, segment, base))
    return worst


if __name__ == "__main__":
    sys.exit(main())
