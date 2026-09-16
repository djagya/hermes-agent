"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

GitHub access always goes through ``gh`` with an explicit token when the
Hermes secret surface has one (``GH_TOKEN`` / ``GITHUB_TOKEN``, profile-scoped
via :mod:`agent.secret_scope`), so a worker whose process environment cannot
see the user's ``gh`` login still reaches the API. Failures are typed
(``capability`` / ``auth_unavailable`` / ``rate_limited`` / ``network`` /
``provider_error`` / ``infra``) instead of one ambiguous null-head ``infra``;
``gh`` stderr is never persisted because it can echo credential material.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from urllib.parse import quote

from agent.secret_scope import UnscopedSecretError, get_secret

logger = logging.getLogger(__name__)

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")

_INFRA_DETAIL = "GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry."


class _GhError(Exception):
    """Typed ``gh`` subprocess failure; ``detail`` is safe for the event log."""

    def __init__(self, classification: str, detail: str):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _gh_auth_env() -> dict[str, str]:
    """Child env for ``gh`` carrying the Hermes-secret-surface token when one exists.

    ``gh`` itself prefers ``GH_TOKEN``/``GITHUB_TOKEN`` over its own stored login,
    so exporting the profile's secret wins over an ambient user login; with no
    secret-surface token the ambient login is kept exactly as before.
    """
    env = dict(os.environ)
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    try:
        token = get_secret("GH_TOKEN") or get_secret("GITHUB_TOKEN")
    except UnscopedSecretError:
        # Multiplex mode with no profile scope installed: reading os.environ here
        # could leak another profile's token, so fail closed as a typed capability
        # result instead of silently skipping authentication.
        raise _GhError(
            "capability",
            "No profile secret scope is active; refusing to read GitHub credentials "
            "from the unscoped process environment. Retry completion from the "
            "profile's own gateway/CLI context.") from None
    if token:
        env["GH_TOKEN"] = token
    return env


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=30, check=True, env=_gh_auth_env())
    except FileNotFoundError:
        raise _GhError("capability", "gh CLI is not installed or not on PATH; install GitHub CLI to use PR completion contracts.") from None
    except subprocess.TimeoutExpired:
        raise _GhError("network", "GitHub API request timed out; retry when the network is available.") from None
    except subprocess.CalledProcessError as exc:
        low = (exc.stderr or "").lower()
        if exc.returncode == 4 or "gh auth login" in low:
            raise _GhError("auth_unavailable", "gh is not authenticated in this context; run `gh auth login` or set GH_TOKEN via the Hermes secret surface.") from None
        if "rate limit" in low:
            raise _GhError("rate_limited", "GitHub API rate limit exhausted; wait for the reset window and retry completion.") from None
        if "http 401" in low or "http 403" in low or "bad credentials" in low:
            raise _GhError("auth_unavailable", "GitHub rejected the acceptance credential (HTTP 401/403); refresh gh authentication or the GH_TOKEN secret.") from None
        if "http 404" in low:
            raise _GhError("provider_error", "GitHub returned 404 for acceptance evidence; check the repository/PR and the credential's repository access.") from None
        logger.warning("gh api failed for %s (rc=%s); stderr suppressed", endpoint, exc.returncode)
        # stderr can echo credential material (bare 40-hex tokens denylists miss),
        # so the persisted detail is fixed and never contains any stderr substring.
        detail = f"GitHub API call failed (rc={exc.returncode})."
        raise _GhError("provider_error", detail) from None
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        if any("Bad credentials" in str(e.get("message", "")) for e in value["errors"] if isinstance(e, dict)):
            raise _GhError("auth_unavailable", "GitHub rejected the acceptance credential; refresh gh authentication or the GH_TOKEN secret.")
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def collect_acceptance(contract: str, published_pr: str | None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){headRefOid baseRefName state
            baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}''' % (
                json.dumps(owner), json.dumps(name), number)
        try:
            pr = _api("graphql", query=query)["data"]["repository"]["pullRequest"]
        except _GhError as exc:
            receipt.update(classification=exc.classification, detail=exc.detail)
            return receipt
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100", paginate=True)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest", paginate=True)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100", paginate=True) for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}")
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except _GhError as exc:
        # Never persist gh stderr (credentials/host details); typed phases stay actionable.
        receipt.update(classification=exc.classification, detail=exc.detail)
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail=_INFRA_DETAIL)
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
