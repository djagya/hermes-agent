# Upstream merge conflict ledger (local candidate)

Base HEAD: `8a237660c2fb70895ac0ea1c64ef66d8bbd2968f`  
Upstream MERGE_HEAD: `6f7a7991bb069db07ae74a479823ce8310f8c7e0`  
Upstream `55a93d8f70` is an ancestor of MERGE_HEAD and remains in the merged history. The cancelled duplicate task lane supplied no changes.

## Subsystem decisions and uncertainty

All paths retained automatic non-overlapping changes from BOTH histories. The table below specifies only the contested hunks: “fork-overlap” means the fork side of those hunks, not a whole-file ours checkout; “upstream-overlap” is similarly local. “Mixed” and “interleaved” indicate hunk-specific or additional semantic edits.

- CI/release/build: upstream supplies release-mode callable jobs, strict result evaluation, Bot Screen container variant, PM-produced sealed environment and SQLite repair. Fork contributes GHCR-specific pipeline gates, selective-lane failure checks, image safety policy and parser/toolbox surface. Merge repairs the CI lane key `bootstrap` and keeps the upstream strict release check. Docker stage graph uses upstream runtime plus explicit fork `test` fixture target; upstream publisher selects `runtime`. Pinned himalaya stage and fork image provenance args retained. The old raw Python installer overlay and second runtime implementation were removed. ROOT decided to preserve the toolbox using a production `sera-toolbox` extra, explicitly selected by the Docker sealed build. It declares previously reviewed fork pins for `PyMuPDF`, `weasyprint`, `python-docx`, `openpyxl`, `yt-dlp` and the previously dev-only `ruff`. `ddgs`, `fal_client` and `faster_whisper` come from existing extras reused by `sera-toolbox`, and `pillow_heif` comes from core. The old raw Python installer overlay and second runtime implementation remain removed. **Open prerequisite:** `uv.lock` must be regenerated through PM in an isolated executor (forbidden here by HERMES-CI-ONLY-v1 and no package-download authority); the Docker build and new image import test are not yet run. No image smoke PASS claimed.
- Kanban/notify: fork-owned attestation, typed holds, fixed notification target and dashboard behavior remain, with upstream source changes retained outside conflicts. Known unresolved product question: fixed target delivers to subscriber but does NOT return the result to the originating conversation. No invented dual-route path. Kanban guidance is gated by `owned_kanban_task()` in `agent/agent_init.py` and `agent/system_prompt.py`, preserving upstream PR #113205 behavior for ordinary interactive sessions.
- Gateway/browser: upstream busy-turn and platform hooks coexist with fork Telegram group gating, supersession, transcript and slot lifetime. Uncertain: profile/gateway integration until exact-head CI.
- Cron/delegation: upstream monitor content goes through `runtime_data_prompt`; fork monitor-state commitment waits until setup gates pass. Fork typed lifecycle verdict remains with upstream bounded referenced-script scanning, including rejection on budget exhaustion. Focused agent/cron/delegate targets required in CI.
- Approvals/security: fork pending-write digest/capability and target byte guard remain; upstream pinned full memory entries, operation-level skill locks and reporting coexist. The CLI apply callback now preserves return payload while satisfying the two-value pending-store callback. Exact-head CI should exercise stale-write, two-homes and concurrent skill/memory paths.
- Config/state: upstream schema, FTS and profile-scoped config changes merged with fork migration decisions. Lockfile aligns to fork `0.21.4` while upstream dependency graph and Python `<3.15` support are retained; lock reconciliation in CI remains required.
- Vault: 1Password opaque handle logic retains fork payment metadata and upstream origin normalization.

## Per-path hunk decisions

| Conflicted path | Hunk resolution |
|---|---|
| `.github/workflows/ci.yaml` | interleaved upstream and fork semantics; see subsystem notes |
| `.github/workflows/docker.yml` | upstream-overlap; fork nonoverlap retained |
| `.github/workflows/install-e2e.yml` | fork-overlap; upstream nonoverlap retained |
| `.github/workflows/js-tests.yml` | upstream-overlap; fork nonoverlap retained |
| `.github/workflows/lint.yml` | both overlapping variants retained |
| `.github/workflows/nix.yml` | upstream-overlap; fork nonoverlap retained |
| `.github/workflows/publish-e2e-evidence.yml` | upstream deletion (workflow removed) |
| `.github/workflows/tests-os.yml` | upstream-overlap; fork nonoverlap retained |
| `.github/workflows/tests.yml` | mixed overlap resolution; nonoverlap retained |
| `Dockerfile` | reconciled stage graph; upstream runtime + fork toolbox |
| `agent/vault_backends/onepassword.py` | interleaved upstream and fork semantics; see subsystem notes |
| `cli.py` | fork-overlap; upstream nonoverlap retained |
| `cron/lifecycle_guard.py` | interleaved upstream and fork semantics; see subsystem notes |
| `cron/scheduler.py` | interleaved upstream and fork semantics; see subsystem notes |
| `gateway/platforms/base.py` | both overlapping variants retained |
| `gateway/run_busy.py` | both overlapping variants retained |
| `gateway/run_turn.py` | fork-overlap; upstream nonoverlap retained |
| `gateway/session_transcript.py` | fork-overlap; upstream nonoverlap retained |
| `hermes_cli/__init__.py` | interleaved upstream and fork semantics; see subsystem notes |
| `hermes_cli/cli_commands_mixin.py` | upstream-overlap; fork nonoverlap retained |
| `hermes_cli/kanban_db.py` | fork-overlap; upstream nonoverlap retained |
| `hermes_cli/kanban_db_notify.py` | fork-overlap; upstream nonoverlap retained |
| `hermes_cli/kanban_pr_acceptance.py` | fork-overlap; upstream nonoverlap retained |
| `hermes_cli/write_approval_commands.py` | interleaved upstream and fork semantics; see subsystem notes |
| `hermes_state_messages.py` | both overlapping variants retained |
| `hermes_state_schema.py` | fork-overlap; upstream nonoverlap retained |
| `hermes_state_search.py` | fork-overlap; upstream nonoverlap retained |
| `package-lock.json` | mixed overlap resolution; nonoverlap retained |
| `plugins/kanban/dashboard/plugin_api.py` | fork-overlap; upstream nonoverlap retained |
| `pyproject.toml` | fork-overlap; upstream nonoverlap retained |
| `tests/agent/test_compression_stall_fallback.py` | upstream-overlap; fork nonoverlap retained |
| `tests/ci/test_classify_changes.py` | fork-overlap; upstream nonoverlap retained |
| `tests/docker/test_immutable_install_permissions.py` | fork-overlap; upstream nonoverlap retained |
| `tests/gateway/test_telegram_group_gating.py` | both overlapping variants retained |
| `tests/hermes_cli/test_approvals_command.py` | fork-overlap; upstream nonoverlap retained |
| `tests/hermes_cli/test_config_effective.py` | fork-overlap; upstream nonoverlap retained |
| `tests/hermes_cli/test_kanban_notify.py` | fork-overlap; upstream nonoverlap retained |
| `tests/hermes_cli/test_local_quickstart.py` | upstream-overlap; fork nonoverlap retained |
| `tests/hermes_cli/test_managed_scope_loaders.py` | upstream-overlap; fork nonoverlap retained |
| `tests/hermes_cli/test_profiles.py` | fork-overlap; upstream nonoverlap retained |
| `tests/hermes_state/test_fts_tool_write_bounds.py` | fork-overlap; upstream nonoverlap retained |
| `tests/hermes_state/test_fts_trigram_subagent_exclusion.py` | fork-overlap; upstream nonoverlap retained |
| `tests/plugins/test_kanban_dashboard_plugin.py` | fork-overlap; upstream nonoverlap retained |
| `tests/tools/test_delegate_timeout_cleanup.py` | mixed overlap resolution; nonoverlap retained |
| `tests/tools/test_launchctl_guard_diagnostic.py` | both overlapping variants retained |
| `tests/tools/test_mcp_oauth.py` | both overlapping variants retained |
| `tests/tools/test_mcp_oauth_manager.py` | fork-overlap; upstream nonoverlap retained |
| `tests/tools/test_skill_manage_batch.py` | fork-overlap; upstream nonoverlap retained |
| `tests/tools/test_skill_size_limits.py` | fork-overlap; upstream nonoverlap retained |
| `tests/tools/test_write_approval.py` | fork-overlap; upstream nonoverlap retained |
| `tools/approval.py` | fork-overlap; upstream nonoverlap retained |
| `tools/approval_smart.py` | fork-overlap; upstream nonoverlap retained |
| `tools/browser_cdp_tool.py` | fork-overlap; upstream nonoverlap retained |
| `tools/browser_supervisor.py` | upstream-overlap; fork nonoverlap retained |
| `tools/browser_tool_session.py` | fork-overlap; upstream nonoverlap retained |
| `tools/code_execution_tool.py` | fork-overlap; upstream nonoverlap retained |
| `tools/kanban_tools.py` | fork-overlap; upstream nonoverlap retained |
| `tools/mcp_tool_lifecycle.py` | fork-overlap; upstream nonoverlap retained |
| `tools/memory_tool.py` | interleaved upstream and fork semantics; see subsystem notes |
| `tools/memory_tool_store.py` | interleaved upstream and fork semantics; see subsystem notes |
| `tools/skill_manager_batch.py` | fork-overlap; upstream nonoverlap retained |
| `tools/skill_manager_tool.py` | interleaved upstream and fork semantics; see subsystem notes |
| `tools/terminal_tool_guards.py` | interleaved upstream and fork semantics; see subsystem notes |
| `tools/write_approval.py` | interleaved upstream and fork semantics; see subsystem notes |
| `uv.lock` | fork-overlap; upstream nonoverlap retained |
| `website/docs/user-guide/features/kanban.md` | fork-overlap; upstream nonoverlap retained |

## Evidence / obligations

Locally inspected all 66 conflict paths and resolved markers, AST-parsed conflicting Python, checked undefined bindings via Ruff F821/F823, and checked unstaged conflict diff for whitespace. These are **static checks only**, not exercised Hermes tests. `55a93d8f70` was independently confirmed as an ancestor of MERGE_HEAD (exit 0). No Docker, gateway, test runner, push, deployment or service action. ROOT must run isolated exact-head CI for focused regression, lock/build, and container lanes before asserting test success.
