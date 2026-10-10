# Upstream merge conflict ledger (sync/v0.21.6-dlz candidate)

Base HEAD: `4347000c0bb8e4c256c0699ef2a591cc873afe0a` (release/v0.21.5-dlz, the pinned image)
Upstream MERGE_HEAD: `818c13be1dc4fd28987e1e881a9408224afd4535` (tag `v0.21.6`, 2026-10-08)
Merge base: `6f7a7991bb069db07ae74a479823ce8310f8c7e0` (the upstream parent of the previous sync, #42)

Same method as #42: one two-parent merge of the upstream release onto the fork line, conflicts
resolved hunk by hunk, everything outside a conflict taken from both histories. 33 paths
conflicted (30 content, 3 modify/delete). The previous ledger (sync onto `6f7a7991bb`) is in git
history at release/v0.21.5-dlz.

## Decisions that change behavior (operator review)

- **Home Assistant left core upstream** (`fb9fc8a3dd`): the gateway platform and the
  `homeassistant` toolset are now the catalog plugin `homeassistant`
  (NousResearch/hermes-homeassistant, catalog sha `ba30cb0c`, v2.0.1). The upstream deletion of
  `plugins/platforms/homeassistant/adapter.py` and `tests/gateway/test_homeassistant.py` is
  accepted. Consequences:
  - The image sets `HERMES_DISABLE_LAZY_INSTALLS=1`, so the left-core migration does NOT fetch the
    plugin at gateway start; it reports "Home Assistant moved out of core … run
    `hermes plugins install homeassistant`". Until that runs on the box, Sera has no HA platform
    or `ha_*` tools. The install lands in `/opt/data/plugins` and survives later recreates.
  - Fork carryover #97 (`9182a191b0`, quiet cold-boot connect grace) lived in the deleted
    adapter. It is not in this candidate; it has to be re-landed in the plugin (a fork of
    hermes-homeassistant, or upstream).
- **Version derivation**: upstream main now carries `0.0.0` and releases are semver tags
  (`v0.21.6`). `scripts/fork-release-version.sh` now takes the highest stable `vX.Y.Z` tag merged
  into HEAD (same rule as `hermes_cli.version_info`) and falls back to the CalVer rule for older
  trees. The fork keeps committing the base: `pyproject.toml` and `uv.lock` say `0.21.6`.
- **CI review-label gate removed** (upstream `059efe8b29`): `review-labels.yml`, the
  `ci_review` / `ci_review_files` / `mcp_catalog` classifier outputs and the rerun job are gone;
  upstream enforces the same paths through `.github/CODEOWNERS`, which names
  `@NousResearch/hermes-agent-core` and therefore does nothing in djagya/hermes-agent. The
  fork's `ci-reviewed` label rule now has no CI enforcement until CODEOWNERS points at a fork
  owner (and the ruleset requires code-owner review) or the gate is restored.
- **Messages schema**: both sides added columns after `display_order` as history event 16
  (fork: `platform_delivery`; upstream: `message_uid`, `absorbed_message_uids`,
  `tool_call_uids`, `tool_call_uid`). Fork stores already hold `platform_delivery`
  physically after `display_order`, so the declaration keeps it there and upstream's four
  columns follow it as event 17. `_reconcile_columns` appends them on first open; the
  `message_uid` backfill runs per open, independent of the fork's held `schema_version<30`
  FTS fence.

## Per-path hunk decisions

| Conflicted path | Resolution |
|---|---|
| `Dockerfile` | fork runtime stage kept; its `HERMES_PYTHON` now the sealed `.venv` python (upstream `48353b2b89`, TUI gateway children) |
| `cron/scheduler.py` | fork `release_fire_claim` import + 3-tuple prompt return; upstream `store_health` / execution-identity imports and `note_cron_skipped` |
| `gateway/kanban_watchers_notifier.py` | both: fork wake-decision contract and upstream `_pin_first` |
| `hermes_cli/cli_init_mixin.py` | upstream localized `t(...)` warning with the fork's `_error` binding |
| `hermes_cli/kanban_pr_acceptance.py` | fork typed `_GhError` phases and secret-surface token for unassigned cards; upstream assignee-profile `gh` identity (`_gh_env`), `auth` receipts and null-repository check for assigned cards |
| `hermes_cli/session_schema_history.py`, `hermes_state_common.py` | see Messages schema above |
| `hermes_state_search.py` | fork: live FTS rebuild stays disabled (upstream's corruption-class logging applies only to the rebuild it guards) |
| `tools/approval.py` | upstream dropped `permanent_capable` (tirith removal); fork `provenance` kept |
| `tools/environments/local.py` | both: upstream Bot Desktop env, then fork `HERMES_OP_CACHE_ONLY` |
| `tools/memory_tool.py`, `tools/memory_tool_store.py` | both: fork write guard / sha binding; upstream `FAILURE_CLASS` tags |
| `tools/write_approval.py` | fork file kept; upstream surface-aware staged hint (#98330), headless inline-prompt skip, and batch-aware skill diff (`_fold_patch`, `_batch_pending_diff`) ported onto it |
| `uv.lock` | relocked with `./hermes pm lock` from the merged `pyproject.toml` |
| `plugins/platforms/homeassistant/adapter.py`, `tests/gateway/test_homeassistant.py` | upstream deletion (left core) |
| `tests/tools/test_browser_use_pm.py` | upstream deletion (browser-use engine moved into the main venv) |
| `.github/actions/detect-changes/action.yml`, `scripts/ci/classify_changes.py` | fork selective outputs (`py_scope`, `py_roots`, `frontend_workspaces`, `os_tests`, `mode`, installer surfaces) plus upstream slow lanes (`docker`, `nix`, `e2e*`, `run-e2e` label, shared-fixture consumers, which also widen the fork selectors); review-gate outputs dropped with upstream |
| `.github/workflows/ci.yaml` | union of outputs; `FORK_LEAN` also turns off `e2e_upgrade` and both Desktop E2E lanes on push/PR; fork zero-job guard extended to the e2e lanes |
| `.github/workflows/tests.yml` | fork sliced standard runners and `scope`/`roots` inputs; upstream `e2e`/`e2e_upgrade`/`strict_acceptance` inputs and job gates |
| `.github/workflows/tests-os.yml`, `windows-install-update-e2e.yml` | fork two-core worker counts and 90 min timeout; upstream test slice and 3600 s file timeout |
| `.github/workflows/label-rerun.yml` | upstream `run-e2e` rerun job |
| tests (`tests/ci/*`, `tests/gateway/test_status.py`, `tests/hermes_cli/*`, `tests/tools/test_write_approval.py`) | both sides' cases; expectations combined where one case gained fields from both; upstream staged-hint tests use distinct facts (the fork pending store deduplicates identical payloads) |
