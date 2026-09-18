#!/usr/bin/env python3
"""Classify a PR's changed files into CI work lanes.

Reads newline-separated changed paths on stdin and writes ``key=value``
booleans (one per lane) to ``$GITHUB_OUTPUT`` and stdout. The
``detect-changes`` composite action consumes them so steps gate on
``if: steps.changes.outputs.<lane> == 'true'``.

Lanes:

* ``python``      — pytest / ruff / ty / footguns.
* ``python_prod`` — Python changes OUTSIDE tests/ — gates jobs that ship or
  run the product (Desktop E2E backend, Docker image) but never import the
  test suite. A tests-only PR keeps ``python`` (pytest must run) while
  skipping those product jobs.
* ``docker_meta`` — Dockerfiles etc.
* ``docker`` — any product change + docker meta
* ``nix``         — ``nix flake check``: the flake inputs and any product change.
* ``frontend``    — TS typecheck matrix + desktop build.
* ``site``        — Docusaurus + generated skill docs.
* ``scan``        — supply-chain scan (Python files, .pth, setup hooks).
* ``deps``        — pyproject.toml dependency bounds check.
* ``uv_lock``     — ``uv lock --check``. Re-resolves the whole graph against
  PyPI, so a diff that touches neither ``pyproject.toml`` nor ``uv.lock``
  must not run it.
* ``npm_lock``    — semantic package-lock.json diff PR comment.
* ``installer``   — PowerShell installer tests (Windows runner).
* ``desktop_updater`` — the Windows desktop-update hand-off script and the
  tests that drive the REAL ``windows.ps1`` (``-SelfTestUi`` / pipe drain /
  retry policy). These are integration tests of a PowerShell process on a
  shared runner; running them on every Python PR made their timing noise
  everyone's problem. They still run on push (fail-open) and whenever the
  script, its siblings, or their tests change.
* ``rust``        — ``cargo test`` for the Tauri bootstrap installer. ``.rs``
  lives under ``apps/``, so without this lane a Rust change matched ``frontend``
  and only the TypeScript matrix ran.
* ``mcp_catalog`` — bundled MCP catalog / installer review.

Selective-execution lanes (fork CI minute optimization — the Python lane is
the dominant cost, so "python=true" is no longer license to run all ~4000
test files on 8 runners):

* ``py_scope``    — ``full`` | ``selective`` | ``none``. ``full`` runs the
  whole suite (8 slices); ``selective`` runs only the resolved test roots in
  ``py_roots``; ``none`` when the python lane is off.
* ``py_roots``    — JSON array of candidate groups, one per changed
  Python-relevant file. Each group is tried against the repo tree by
  ``scripts/ci/select_test_roots.py``: exact-twin test globs PLUS the mirrored
  subsystem subtree (union, deduped). Resolution failure of the whole set
  fails closed to ``full`` — never to zero.
* ``frontend_workspaces`` — JSON array of npm workspace dirs whose
  ``check*`` scripts must run. Empty array = all workspaces (fail-open).
* ``os_tests``    — run the macOS/Windows lanes (10x/2x minute weight). Armed
  by the platform surfaces those suites actually exercise (tools/, hermes_cli/,
  scripts/, apps/, installer/desktop-updater, manifest/conftest) or fail-open;
  a gateway/agent/cron-only change skips them on ordinary PRs. Full dispatch
  and push fail-open still run them.
* ``mode``        — ``full`` when the classifier failed open (empty diff,
  ``.github/`` change, push/dispatch without a computable diff), else
  ``selective``. Surfaced in the detect job summary for audit.

Docker is not a lane — it builds on push-to-main and release only,
never per-PR.

Contract — *fail open, never closed*. We may run a lane we didn't need, but
must never skip one a change could break:

* An empty diff, or any ``.github/`` change, runs everything.
* ``python`` is a denylist: skipped only when *every* file is provably prose
  or a frontend-only package; an unrecognized path keeps it on.
* ``skills/`` (incl. ``SKILL.md``) is python-relevant — the skill-doc tests
  read that tree, so a doc-looking edit can still break Python.
* ``nix/``, ``flake.nix`` and ``flake.lock`` are the exception the other way:
  only the flake reads them, so they skip the Python lanes and run ``nix``
  alone. ``pyproject.toml`` and ``uv.lock`` are flake inputs too, but the
  packaging tests read them, so they keep every Python lane.
* ``website/static/oauth/`` is python-relevant too: it publishes the OAuth
  Client ID Metadata Document that ``tests/tools/test_mcp_cimd.py`` checks
  against the pinned callback ports in ``tools/mcp_oauth.py``.
* ``website/docs/`` and ``website/scripts/`` are python-relevant for the same
  reason: the docs tree generates ``llms.txt``, and
  ``tests/website/test_generate_llms_txt.py`` asserts every page reaches it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_FRONTEND = ("ui-tui/", "web/", "apps/")  # TS typecheck-matrix packages
# Shipped page outside those packages, exercised by the desktop Electron suite.
_FRONTEND_FILES = {"scripts/desktop-update/ui.html"}
_ROOT_NPM = {"package.json", "package-lock.json"}  # shifts every package's tree
_DOCKER_META = ("docker/", ".hadolint.yml", "Dockerfile") # docker setup
_NIX_PATHS = ("nix/",) # nix files
_NIX_FILES = {"flake.nix", "flake.lock"} # base nix files
_SITE = ("website/", "skills/", "optional-skills/")  # docs site + skill pages
# Prose/frontend trees that can't touch Python. skills/ is excluded on purpose.
_PY_SKIP = ("docs/", "website/") + _FRONTEND
# Published artifacts that live under website/ but that Python asserts about.
# The OAuth Client ID Metadata Document is cross-checked against the pinned
# callback ports in tools/mcp_oauth.py, so editing it alone must still run the
# Python lane — otherwise dropping a redirect URI goes green here and breaks
# every CIMD login on main.
# website/docs/ and website/scripts/ are asserted about the same way. The docs
# tree generates llms.txt — the index every LLM (Hermes included, via the
# hermes-agent skill) reads to learn what Hermes can do — and
# tests/website/test_generate_llms_txt.py holds every page to appearing in it.
# Skipping Python on a docs-only PR is how the index drifted to 53% coverage.
_PY_RELEVANT_SITE = (
    "website/static/oauth/",
    "website/docs/",
    "website/scripts/",
)
# Cross-language contract files: data committed under a frontend tree that a
# pytest pins against the Python side (emitter inventory, command registry).
# Editing only the JSON in an apps/-only PR would otherwise skip the one test
# that can catch the drift, so these force the Python lane too.
_PY_RELEVANT_CONTRACT_FILES = {
    # tests/tui_gateway/contracts/test_generated.py (rendered from tui_gateway/contracts)
    "apps/shared/src/gateway-contract.generated.ts",
    "apps/shared/src/gateway-contract.openrpc.json",
    # tests/hermes_cli/test_desktop_slash_registry.py
    "apps/desktop/src/lib/desktop-slash-registry.json",
}

# CI-sensitive files: eslint config, workflow files, composite actions.
# Changes here can influence what code the autofix job executes and pushes to
# main, so they require explicit maintainer review (ci-reviewed label).
#
# package.json is deliberately NOT listed here: npm scripts only execute on the
# unprivileged generate-patch runner (contents: read), never on the privileged
# apply-patch job. The two-job split means a malicious package.json script
# can't get push access — it runs on an ephemeral runner with zero write perms.
_CI_REVIEW_FILES = {
    ".prettierrc",
}
_CI_REVIEW_PATHS = (".github/workflows/", ".github/actions/")

# Supply-chain scan: files that can execute code at install/import time.
_SCAN_EXTS = (".py", ".pth")
_SCAN_FILES = {"setup.cfg", "pyproject.toml"}

# MCP catalog files that require explicit security review.
_MCP_CATALOG_PATHS = ("optional-mcps/",)
_MCP_CATALOG_FILES = {"hermes_cli/mcp_catalog.py"}

# Windows installer + its PowerShell tests. These only run on a Windows runner,
# so they get their own lane rather than riding along with ``python``.
_INSTALLER_PATHS = ("scripts/tests/",)
_INSTALLER_FILES = {"scripts/install.ps1", "scripts/install.cmd"}

# Windows desktop-update hand-off (scripts/desktop-update/windows.ps1 + the
# Electron side that launches it) and the pytest files that spawn it.
_DESKTOP_UPDATER_PATHS = ("scripts/desktop-update/",)
_DESKTOP_UPDATER_TEST_PREFIX = "tests/scripts/desktop_update/"
_DESKTOP_UPDATER_FILES = {
    "apps/desktop/electron/updater-process.ts",
    "apps/desktop/electron/managed-ssh-update.ts",
    "tests/conftest.py",
    "pyproject.toml",
}

# Rust crates — currently just the Tauri bootstrap installer (Hermes-Setup).
# These live under ``apps/``, so before this lane existed a ``.rs`` edit matched
# ``frontend`` and nothing more: the TypeScript matrix built, cargo never ran,
# and the crate's unit tests had never executed in CI at all.
_RUST_PATHS = ("apps/bootstrap-installer/src-tauri/",)
_RUST_FILENAMES = {"Cargo.toml", "Cargo.lock"}

# ── Selective-execution mapping ─────────────────────────────────────────────
# Source prefix → mirrored test subtree(s). The resolver
# (scripts/ci/select_test_roots.py) runs the UNION of the exact-twin test
# globs and every mirrored subtree that exists — the subtree is the coverage
# backbone, twins only add cross-directory hits (e.g. tools/foo.py also has
# tests/test_foo.py at the top level). Over-inclusion is the safe direction;
# anything not provably mapped fails open to the full suite.
_PY_TEST_SUBTREES = {
    "agent/": ("tests/agent/",),
    "gateway/": ("tests/gateway/", "tests/relay/"),
    "hermes_cli/": ("tests/hermes_cli/",),
    "tools/": ("tests/tools/",),
    "cron/": ("tests/cron/",),
    "plugins/": ("tests/plugins/",),
    "skills/": ("tests/skills/",),
    "optional-skills/": ("tests/skills/",),
    "scripts/ci/": ("tests/ci/",),
    "evals/": ("tests/evals/",),
    "acp_adapter/": ("tests/acp_adapter/", "tests/acp/"),
    # Python-asserted site trees (see _PY_RELEVANT_SITE) → their tiny suite.
    "website/docs/": ("tests/website/",),
    "website/scripts/": ("tests/website/",),
    "website/static/oauth/": ("tests/website/",),
}

# Changes here reshape the whole suite's collection or the runner itself, so
# they always fail open to the full suite regardless of where they live.
_PY_FULL_PREFIXES = ("tests/fakes/", "tests/fixtures/")
_PY_FULL_FILES = {"tests/conftest.py"}

# npm workspace selection for the JS lane. Anything frontend-relevant that is
# not listed here (root package.json / package-lock.json, eslint configs at
# unknown paths, ...) selects ALL workspaces — fail-open is the empty list.
_FRONTEND_WORKSPACE_MAP = {
    "apps/desktop/": ("apps/desktop", "apps/shared"),
    # apps/desktop imports apps/shared, so a shared change must re-check it.
    "apps/shared/": ("apps/shared", "apps/desktop"),
    "apps/bootstrap-installer/": ("apps/bootstrap-installer",),
    "web/": ("web",),
    "tests-js/": ("tests-js",),
    "ui-tui/": ("ui-tui",),
}
_FRONTEND_FILE_WORKSPACES = {
    # Shipped updater page, exercised by the desktop Electron suite.
    "scripts/desktop-update/ui.html": ("apps/desktop",),
}

# Surfaces the macOS/Windows-only suites (see scripts/ci/list_os_marked_tests.py
# and the _OS_MARKS block in tests/conftest.py) actually exercise: terminal
# backends, CLI process/update machinery, installer + desktop-update scripts,
# the desktop Electron shell. An agent/gateway/cron/plugins-only change runs
# no OS-marked subject, so paying the 10x macOS minute weight there is waste.
_OS_SOURCE_PREFIXES = (
    "tools/",
    "hermes_cli/",
    "scripts/",
    "apps/",
    "tui_gateway/",
)
_OS_TEST_PREFIXES = (
    "tests/tools/",
    "tests/hermes_cli/",
    "tests/desktop/",
    "tests/computer_use/",
    "tests/install/",
)
_OS_FILES = {"pyproject.toml", "uv.lock", "setup.py", "tests/conftest.py"}

def _is_docs(p: str) -> bool:
    if p.startswith(("skills/", "optional-skills/")):
        return False
    return p.endswith((".md", ".mdx")) or p.startswith("docs/") or p.startswith("LICENSE")


def _is_nix(p: str) -> bool:
    return p.startswith(_NIX_PATHS) or p in _NIX_FILES


def _py_irrelevant(p: str) -> bool:
    if p.startswith(_PY_RELEVANT_SITE) or p in _PY_RELEVANT_CONTRACT_FILES:
        return False
    return (
        _is_docs(p)
        or p in _ROOT_NPM
        or p.startswith(_PY_SKIP)
        or p.startswith(_DOCKER_META)
        or _is_nix(p)
    )


def _py_test_only(p: str) -> bool:
    """Is ``p`` inside the test suite (never shipped / imported by the product)?

    Product jobs (Desktop E2E's ``hermes serve`` backend, the Docker image)
    run installed code — nothing under ``tests/`` is packaged or importable
    there. scripts/run_tests.sh and run_tests_parallel.py are deliberately
    NOT test-only: they are runner infrastructure, and a bad edit there can
    mask real failures, so they stay conservative (python_prod=true).
    """
    return p.startswith("tests/")


def _is_scan(p: str) -> bool:
    return p.endswith(_SCAN_EXTS) or p in _SCAN_FILES


def _is_mcp_catalog(p: str) -> bool:
    return p.startswith(_MCP_CATALOG_PATHS) or p in _MCP_CATALOG_FILES


def _is_installer(p: str) -> bool:
    return p.startswith(_INSTALLER_PATHS) or p in _INSTALLER_FILES


def _is_desktop_updater(p: str) -> bool:
    return (
        p.startswith(_DESKTOP_UPDATER_PATHS)
        or p.startswith(_DESKTOP_UPDATER_TEST_PREFIX)
        or p in _DESKTOP_UPDATER_FILES
    )


def _is_rust(p: str) -> bool:
    return (
        p.endswith(".rs")
        or p.startswith(_RUST_PATHS)
        or os.path.basename(p) in _RUST_FILENAMES
    )


def _is_ci_review(p: str) -> bool:
    if p in _CI_REVIEW_FILES or p.startswith(_CI_REVIEW_PATHS):
        return True
    # Any eslint config file at any path — eslint configs can define custom
    # fix functions that execute arbitrary code, so they all require review.
    return os.path.basename(p).startswith("eslint.config.")


def _py_root_group(p: str) -> list[str] | None:
    """Candidate test roots covering a change to ``p``, or None for full-suite.

    A group lists glob-capable candidates the resolver expands against the
    repo tree; the union of everything that exists is what runs. Returning
    None means "this file can affect anything" — the caller fails open to
    the full suite. Only ever reached for files that are NOT
    ``_py_irrelevant``.
    """
    if p in _PY_FULL_FILES or p.startswith(_PY_FULL_PREFIXES):
        return None
    if p.startswith("tests/"):
        # A test file's coverage is itself. conftest/fakes/fixtures are
        # handled above; runner infrastructure under scripts/ never lands
        # here (it is not under tests/).
        return [p]
    subtrees: tuple[str, ...] | None = None
    for prefix, mapped in _PY_TEST_SUBTREES.items():
        if p.startswith(prefix):
            subtrees = mapped
            break
    if subtrees is None:
        # Root-level hub modules (run_agent.py, cli.py, model_tools.py,
        # hermes_state*.py, ...), scripts/ outside ci/, tui_gateway/, and
        # anything unrecognized are imported broadly enough that no subtree
        # is honest coverage — run everything.
        return None
    group: list[str] = []
    if p.endswith(".py"):
        stem = os.path.basename(p)[:-3]
        # Facade/sibling families (hermes_state.py + hermes_state_*.py) share
        # test stems, so the twin glob carries a trailing '*'.
        for subtree in subtrees:
            group.append(f"{subtree}test_{stem}*.py")
        group.append(f"tests/test_{stem}*.py")
    group.extend(subtrees)
    return group


def _frontend_workspaces(files: list[str]) -> list[str] | None:
    """Workspaces whose check scripts a frontend change can affect.

    Returns None when any frontend-relevant file is unmapped — the empty-list
    output (all workspaces) is the fail-open encoding, so None collapses to
    it in the caller.
    """
    selected: set[str] = set()
    for f in files:
        frontend_relevant = (
            f.startswith(_FRONTEND) or f in _ROOT_NPM or f in _FRONTEND_FILES
        )
        if not frontend_relevant:
            continue
        if f in _ROOT_NPM:
            return None
        mapped: tuple[str, ...] | None = _FRONTEND_FILE_WORKSPACES.get(f)
        if mapped is None:
            for prefix, workspaces in _FRONTEND_WORKSPACE_MAP.items():
                if f.startswith(prefix):
                    mapped = workspaces
                    break
        if mapped is None:
            return None
        selected.update(mapped)
        # A package under ui-tui/packages/<pkg>/ runs its own checks beside
        # the ui-tui umbrella package.
        if f.startswith("ui-tui/packages/"):
            parts = f.split("/")
            if len(parts) > 2 and parts[2]:
                selected.add(f"ui-tui/packages/{parts[2]}")
    return sorted(selected)


def _is_os_surface(p: str) -> bool:
    base = os.path.basename(p).lower()
    return (
        p.startswith(_OS_SOURCE_PREFIXES)
        or p.startswith(_OS_TEST_PREFIXES)
        or p in _OS_FILES
        or "windows" in base
        or "macos" in base
    )


def ci_review_files(files: list[str]) -> list[str]:
    """Return the CI-sensitive paths that need maintainer review."""
    return sorted({f.strip() for f in files if f.strip() and _is_ci_review(f.strip())})


def classify(files: list[str]) -> dict[str, object]:
    """Map changed paths to ``{lane: should_run}`` plus selection outputs.

    Boolean lanes gate sub-workflows; string lanes (``py_scope``, ``py_roots``,
    ``frontend_workspaces``, ``mode``) steer how the gated work executes.
    """
    files = [f.strip() for f in files if f.strip()]
    python = any(not _py_irrelevant(f) for f in files)
    python_prod = any(not _py_irrelevant(f) and not _py_test_only(f) for f in files)
    frontend = any(
        f.startswith(_FRONTEND) or f in _ROOT_NPM or f in _FRONTEND_FILES
        for f in files
    )
    deps = any(f == "pyproject.toml" for f in files)
    npm_lock = any(f.split("/")[-1] == "package-lock.json" for f in files)
    docker_meta = any(f.startswith(_DOCKER_META) for f in files)
    # Fail-open trigger: no diff to classify, or CI itself changed — every
    # lane runs and every selector is at its broadest.
    fail_open = not files or any(f.startswith(".github/") for f in files)

    # Selective Python scope: any python-relevant file without an honest
    # subtree mapping pulls the whole suite back in.
    py_groups: list[list[str]] = []
    py_full = True
    if python and not fail_open:
        py_full = False
        for f in files:
            if _py_irrelevant(f):
                continue
            group = _py_root_group(f)
            if group is None:
                py_full = True
                py_groups = []
                break
            py_groups.append(group)
    if not python:
        py_scope: str = "none"
        py_roots: list[list[str]] = []
    elif py_full:
        py_scope = "full"
        py_roots = [["tests"]]
    else:
        py_scope = "selective"
        py_roots = py_groups

    workspaces = _frontend_workspaces(files)
    if fail_open or not frontend or workspaces is None:
        # Empty array = all workspaces (fail-open encoding).
        frontend_workspaces: list[str] = []
    else:
        frontend_workspaces = workspaces

    os_tests = (
        fail_open
        or any(_is_os_surface(f) for f in files)
        or any(_is_installer(f) for f in files)
        or any(_is_desktop_updater(f) for f in files)
    )

    ret: dict[str, object] = {
        "python": python,
        "python_prod": python_prod,
        "docker": docker_meta or python_prod or frontend,
        "docker_meta": docker_meta,
        "frontend": frontend,
        "site": any(f.startswith(_SITE) for f in files),
        "scan": any(_is_scan(f) for f in files),
        "deps": deps,
        "uv_lock": any(f in ("pyproject.toml", "uv.lock") for f in files),
        "npm_lock": npm_lock,
        "installer": any(_is_installer(f) for f in files),
        "desktop_updater": any(_is_desktop_updater(f) for f in files),
        "rust": any(_is_rust(f) for f in files),
        "mcp_catalog": any(_is_mcp_catalog(f) for f in files),
        "ci_review": any(_is_ci_review(f) for f in files),
        "nix": python_prod or frontend or any(_is_nix(f) for f in files),
        "py_scope": py_scope,
        "py_roots": json.dumps(py_roots),
        "frontend_workspaces": json.dumps(frontend_workspaces),
        "os_tests": os_tests,
        "mode": "full" if fail_open else "selective",
    }
    if fail_open:
        ret["python"] = True
        ret["python_prod"] = True
        ret["docker"] = True
        ret["docker_meta"] = True
        ret["frontend"] = True
        ret["site"] = True
        ret["scan"] = True
        ret["deps"] = True
        ret["uv_lock"] = True
        ret["npm_lock"] = True
        ret["installer"] = True
        ret["desktop_updater"] = True
        ret["rust"] = True
        ret["nix"] = True
        ret["ci_review"] = True
        ret["py_scope"] = "full"
        ret["py_roots"] = json.dumps([["tests"]])
        ret["frontend_workspaces"] = json.dumps([])
        ret["os_tests"] = True

        # explicitly skip mcp catalog here. it's not needed unless those files are modified.
    return ret


def _pull_request_number() -> str | None:
    """Read the PR number from the Actions event payload, if present."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return None
    try:
        with open(event_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    number = (payload.get("pull_request") or {}).get("number")
    return str(number) if number else None


def pull_request_changed_files() -> list[str]:
    """Recover the PR file list when the compare API returned nothing.

    ``detect-changes`` calls ``repos/.../compare/base...head`` with raw SHAs.
    A fork force-push can 404 for ~30s until GitHub attaches the new head SHA
    to the base repo, so the action fails open with an empty file list. That
    forces ``ci_review=true`` and blocks the PR on a ``ci-reviewed`` label
    even when no CI-sensitive file changed.

    The pull-request files endpoint already knows the PR's files (it is how
    this action used to classify), so use it as a fallback on pull_request
    events only. Push/dispatch keep the empty-diff fail-open.
    """
    if os.environ.get("EVENT_NAME") != "pull_request":
        return []
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    pr = _pull_request_number()
    if not repo or not pr:
        return []
    try:
        completed = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/pulls/{pr}/files",
                "--jq",
                ".[].filename",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def main() -> int:
    files = sys.stdin.read().splitlines()
    if not any(f.strip() for f in files):
        recovered = pull_request_changed_files()
        if recovered:
            print(
                f"compare API returned no files; recovered {len(recovered)} "
                "path(s) from the pull request files endpoint",
                file=sys.stderr,
            )
            files = recovered
    lanes = classify(files)
    out = "\n".join([
        *(
            f"{key}={str(value).lower() if isinstance(value, bool) else value}"
            for key, value in lanes.items()
        ),
        f"ci_review_files={json.dumps(ci_review_files(files))}",
    ])
    if dest := os.environ.get("GITHUB_OUTPUT"):
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(out + "\n")
    print(out)  # echo for local runs + CI step logs
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
