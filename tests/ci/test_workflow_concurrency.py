"""Concurrency contract for the reusable test workflows.

The fork's standard flow (PR #21) runs an ordinary PR's CI and an explicit
full ``workflow_dispatch`` on the same branch/head at the same time: the PR
run is the merge-gating evidence, the dispatch run is the pre-upstream
full-validation receipt. The reusable Python and OS workflows must let
neither class of run cancel the other:

- stale ``pull_request`` runs cancel each other, scoped per PR number;
- ``workflow_dispatch`` / ``push`` runs queue in their own per-ref group;
- PR and dispatch/push runs never share a group, so they cannot kill each
  other regardless of which arrived first.

Live collision that motivated this contract (fork, 2026-09-17): with a
single ``tests-<ref>`` group and unconditional ``cancel-in-progress: true``,
the ``Tests`` call of manual dispatch run 35211892455 joined the PR run's
group and SIGTERM'd its slice 3/8 at 82.8% with zero test failures — a
false red on the merge-gating evidence (PR run 35211816101).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# (workflow file, concurrency group prefix)
WORKFLOWS = [
    (".github/workflows/tests.yml", "tests"),
    (".github/workflows/tests-os.yml", "tests-os"),
]

BRANCH_REF = "refs/heads/ci/selective-fork-ci"


def _concurrency(rel: str) -> dict:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8"))
    conc = doc.get("concurrency")
    assert isinstance(conc, dict), f"{rel}: top-level concurrency block missing"
    return conc


def _atom(expr: str, ctx: dict):
    expr = expr.strip()
    if expr.startswith("'") and expr.endswith("'"):
        return expr[1:-1]
    if "==" in expr:
        left, right = (part.strip() for part in expr.split("==", 1))
        return _atom(left, ctx) == _atom(right, ctx)
    # Bare GitHub context path, e.g. github.event.pull_request.number.
    paths = {
        "github.event.pull_request.number": ctx.get("pr_number"),
        "github.event_name": ctx["event"],
        "github.ref": ctx["ref"],
    }
    if expr not in paths:
        raise AssertionError(
            f"unmodeled expression path {expr!r} — extend _atom or the "
            f"concurrency contract changed shape"
        )
    return paths[expr]


def _eval(expr: str, ctx: dict):
    """Evaluate a workflow value that is either one whole ``${{ ... }}``
    expression (native type preserved, e.g. ``cancel-in-progress``) or a
    template string with embedded ``${{ ... }}`` segments (string
    interpolation, e.g. the group). Supported inner syntax: ``a || b``
    fallback and ``x == 'literal'``."""
    if not isinstance(expr, str):
        pytest.fail(
            f"concurrency value {expr!r} is a static literal — the contract "
            f"requires event-aware expressions"
        )
    whole = expr.strip()
    if whole.startswith("${{") and whole.endswith("}}") and "${{" not in whole[3:]:
        return _eval_inner(whole[3:-2], ctx)

    out: list[str] = []
    i = 0
    while True:
        start = expr.find("${{", i)
        if start == -1:
            out.append(expr[i:])
            return "".join(out)
        end = expr.find("}}", start)
        assert end != -1, f"{expr!r}: unterminated expression"
        out.append(expr[i:start])
        out.append(str(_eval_inner(expr[start + 3 : end], ctx)))
        i = end + 2


def _eval_inner(inner: str, ctx: dict):
    inner = inner.strip()
    if "||" in inner:
        left, right = (part.strip() for part in inner.split("||", 1))
        left_val = _atom(left, ctx)
        return left_val if left_val else _atom(right, ctx)
    return _atom(inner, ctx)


def _ctx(event: str, ref: str = BRANCH_REF, pr_number: int | None = None) -> dict:
    return {"event": event, "ref": ref, "pr_number": pr_number}


@pytest.mark.parametrize("rel,prefix", WORKFLOWS)
def test_group_is_pr_scoped_with_ref_fallback(rel: str, prefix: str):
    conc = _concurrency(rel)
    group_expr = conc["group"]

    assert group_expr.startswith(f"{prefix}-"), (
        f"{rel}: group must keep its {prefix}- prefix so the lane never "
        f"shares a concurrency group with another workflow"
    )
    pr = _eval(group_expr, _ctx("pull_request", pr_number=21))
    other_pr = _eval(group_expr, _ctx("pull_request", pr_number=22))
    dispatch = _eval(group_expr, _ctx("workflow_dispatch"))
    push = _eval(group_expr, _ctx("push", ref="refs/heads/main"))

    assert pr == f"{prefix}-21"
    assert other_pr == f"{prefix}-22"
    assert dispatch == f"{prefix}-{BRANCH_REF}"
    assert push == f"{prefix}-refs/heads/main"


@pytest.mark.parametrize("rel,prefix", WORKFLOWS)
def test_cancel_is_limited_to_stale_pull_request_runs(rel: str, prefix: str):
    conc = _concurrency(rel)

    assert _eval(conc["cancel-in-progress"], _ctx("pull_request", pr_number=21)) is True
    assert _eval(conc["cancel-in-progress"], _ctx("workflow_dispatch")) is False
    assert _eval(conc["cancel-in-progress"], _ctx("push", ref="refs/heads/main")) is False


@pytest.mark.parametrize("rel,prefix", WORKFLOWS)
def test_full_dispatch_neither_kills_nor_is_killed_by_pr_evidence(
    rel: str, prefix: str
):
    """The regression: ordinary PR CI and a manual full dispatch on the same
    branch/head must be able to run concurrently without cancelling each
    other."""
    conc = _concurrency(rel)

    pr_group = _eval(conc["group"], _ctx("pull_request", pr_number=21))
    dispatch_group = _eval(conc["group"], _ctx("workflow_dispatch"))

    assert pr_group != dispatch_group, (
        f"{rel}: PR and workflow_dispatch runs on the same head share "
        f"concurrency group {pr_group!r} — one can cancel the other's "
        f"evidence"
    )
    assert _eval(conc["cancel-in-progress"], _ctx("workflow_dispatch")) is False, (
        f"{rel}: a workflow_dispatch run must queue, never cancel"
    )


@pytest.mark.parametrize("rel,prefix", WORKFLOWS)
def test_stale_pr_runs_still_collapse_per_pr(rel: str, prefix: str):
    """The optimization the event-aware policy must preserve: repeated
    pushes to one PR supersede the PR's earlier run."""
    conc = _concurrency(rel)

    first = _eval(conc["group"], _ctx("pull_request", pr_number=21))
    second = _eval(conc["group"], _ctx("pull_request", pr_number=21))

    assert first == second
    assert _eval(conc["cancel-in-progress"], _ctx("pull_request", pr_number=21)) is True


@pytest.mark.parametrize("rel,prefix", WORKFLOWS)
def test_dispatch_and_push_runs_queue_instead_of_cancelling(rel: str, prefix: str):
    """Full-validation evidence is never discarded: same-group runs of the
    non-PR classes queue behind each other."""
    conc = _concurrency(rel)

    d1 = _eval(conc["group"], _ctx("workflow_dispatch"))
    d2 = _eval(conc["group"], _ctx("workflow_dispatch"))
    p1 = _eval(conc["group"], _ctx("push", ref="refs/heads/main"))
    p2 = _eval(conc["group"], _ctx("push", ref="refs/heads/main"))

    assert d1 == d2 and p1 == p2
    assert _eval(conc["cancel-in-progress"], _ctx("workflow_dispatch")) is False
    assert _eval(conc["cancel-in-progress"], _ctx("push", ref="refs/heads/main")) is False
