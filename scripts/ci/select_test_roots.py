#!/usr/bin/env python3
"""Resolve the classifier's selective test roots into a concrete run plan.

Reads the ``py_scope`` / ``py_roots`` outputs of ``classify_changes.py`` (via
environment or arguments), expands the candidate groups against the real repo
tree, and emits a matrix plan for ``.github/workflows/tests.yml`` to
``$GITHUB_OUTPUT`` (and stdout):

* ``matrix`` — JSON array of ``{"slice": "I/N", "paths": "a:b:c"}`` entries.
  Each slice job passes ``paths`` to the test runner as ``HERMES_TEST_PATHS``
  and ``slice`` as ``--slice I/N``; every slice job re-discovers the files
  under those roots itself and runs only its own duration-balanced share.
  (This is deliberately NOT the regressed pre-computed ``--files`` split —
  no file list crosses a job boundary, only discovery roots.)

Contract — fail closed to broad, never to zero:

* ``full`` scope, an unreadable/empty plan, or a selective resolution whose
  candidates all miss the tree => the full-suite 8-slice plan.
* A selective plan always appends ``tests/ci/`` so the CI machinery's own
  tests ride every selective run (they are the guard rails for this file's
  callers, and they are cheap).
* The slice count scales with the resolved file count (~500 files per slice,
  matching the full suite's 8-way balance) and never exceeds 8.

Only used by the Linux lane; ``:`` path joining matches
``HERMES_TEST_PATHS``'s POSIX form.
"""

from __future__ import annotations

import glob
import json
import math
import os
import sys

_FULL_SLICES = 8
_FILES_PER_SLICE = 500
# Selective runs always include the CI self-tests: a bad edit to the
# classifier, the resolver, or the workflows must not be able to ship on a
# green selective run that never executed the guards for those files.
_ALWAYS_INCLUDE = ("tests/ci/",)


def _count_files(root: str, repo_root: str) -> int:
    """Approximate the runner's discovery: test_*.py under dirs, files as-is."""
    path = os.path.join(repo_root, root)
    if os.path.isfile(path):
        return 1
    if os.path.isdir(path):
        n = 0
        for _dirpath, _dirnames, filenames in os.walk(path):
            n += sum(1 for name in filenames if name.startswith("test_") and name.endswith(".py"))
        return n
    return 0


def resolve_roots(groups: list[list[str]], repo_root: str) -> list[str]:
    """Expand candidate groups to the union of roots that exist on disk.

    Each candidate is a directory, a file, or a glob pattern. Existing
    directories and files are kept as roots verbatim (discovery happens in
    the runner); globs expand to the matched files. A candidate that matches
    nothing contributes nothing — only an entirely empty union is a failure
    (handled by the caller, which falls back to the full suite).
    """
    roots: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        normalized = path.rstrip("/") if os.path.isdir(os.path.join(repo_root, path)) else path
        key = normalized.rstrip("/")
        if key not in seen:
            seen.add(key)
            roots.append(path)

    for group in groups:
        for candidate in group:
            if glob.has_magic(candidate):
                for match in sorted(glob.glob(os.path.join(repo_root, candidate))):
                    if os.path.isfile(match):
                        _add(os.path.relpath(match, repo_root))
            elif os.path.exists(os.path.join(repo_root, candidate)):
                _add(candidate)
    for extra in _ALWAYS_INCLUDE:
        if os.path.exists(os.path.join(repo_root, extra)):
            _add(extra)
    return roots


def build_plan(roots: list[str], repo_root: str) -> list[dict[str, str]]:
    """Build the matrix entries for the given roots."""
    total = sum(_count_files(root, repo_root) for root in roots)
    slices = max(1, min(_FULL_SLICES, math.ceil(total / _FILES_PER_SLICE)))
    joined = ":".join(roots)
    return [
        {"slice": f"{i}/{slices}", "paths": joined}
        for i in range(1, slices + 1)
    ]


def full_plan() -> list[dict[str, str]]:
    return [
        {"slice": f"{i}/{_FULL_SLICES}", "paths": "tests"}
        for i in range(1, _FULL_SLICES + 1)
    ]


def plan(scope: str, py_roots_json: str, repo_root: str) -> tuple[list[dict[str, str]], str]:
    """Return ``(matrix, note)`` for one detect output pair.

    ``note`` is a human-readable line for the step summary so the selected
    scope is auditable from the run page.
    """
    if scope != "selective":
        return full_plan(), f"scope={scope!r}: full suite ({_FULL_SLICES} slices of tests/)"
    try:
        groups = json.loads(py_roots_json) if py_roots_json.strip() else []
    except json.JSONDecodeError:
        groups = []
    if not isinstance(groups, list) or not all(isinstance(g, list) for g in groups):
        groups = []
    roots = resolve_roots(groups, repo_root)
    if not roots or roots == list(_ALWAYS_INCLUDE):
        # Every candidate missed the tree (mapping drift, renamed subtree):
        # run everything rather than report green over nothing.
        return full_plan(), (
            "selective resolution found no matching roots beyond the always-included "
            "CI self-tests — failing closed to the full suite"
        )
    matrix = build_plan(roots, repo_root)
    total = sum(_count_files(root, repo_root) for root in roots)
    return matrix, (
        f"selective: {len(roots)} root(s), ~{total} test file(s), "
        f"{len(matrix)} slice(s): {', '.join(roots)}"
    )


def main() -> int:
    repo_root = os.environ.get("REPO_ROOT") or os.getcwd()
    scope = os.environ.get("PY_SCOPE", "full")
    py_roots_json = os.environ.get("PY_ROOTS", "")
    matrix, note = plan(scope, py_roots_json, repo_root)
    encoded = json.dumps(matrix)
    print(f"▶ test plan: {note}")
    if dest := os.environ.get("GITHUB_OUTPUT"):
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(f"matrix={encoded}\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## Test scope\n\n{note}\n\n```json\n{encoded}\n```\n")
    print(f"matrix={encoded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
