# Upstream merge conflict ledger (sync/v0.21.7 candidate)

Base HEAD: `a0e83b328a7b9c8d9d542f8d6bd28b41120ec690` (release/v0.21.6-dlz, the pinned image)
Upstream MERGE_HEAD: `71d34bad1045610252eda02ee79549436896e784` (tag `v0.21.7`, 2026-10-11)
Merge base: `818c13be1dc4fd28987e1e881a9408224afd4535` (tag `v0.21.6`, the previous sync)

Same method as the v0.21.6 sync: one two-parent merge of the upstream release onto the fork
line, conflicts resolved hunk by hunk, everything outside a conflict taken from both histories.
31 paths conflicted, all content conflicts. The previous ledger (sync onto `818c13be1d`) is in
git history at release/v0.21.6-dlz.

Unchanged by this release: config schema (`_config_version` 50,
`hermes_cli/config_defaults.py`) and state schema (`SCHEMA_VERSION` 31,
`hermes_state_common.py`). Upstream's managed-scope files changed only by lint fixes.

## Decisions that change behavior (operator review)

- **Strict ruff is upstream's default now** (new `ruff.toml`, upstream `d68bb0c9ae`). Most
  conflicts are upstream's automatic typing rewrite (`List`/`Dict` -> `list`/`dict`) meeting a
  fork change on the same line. The fork side wins and gets the same rewrite. Fork-authored
  code outside conflicts got the same automatic fixes in a follow-up commit, plus
  `check=False` on `subprocess.run`, unused unpack targets and merged `isinstance` calls.
  `plugins/homeassistant/**` is excluded from lint in `ruff.toml`: it is vendored at its
  catalog pin and `test_vendored_provenance.py` pins its bytes.
- **`cli.py` no longer shadows upstream's config loaders.** Since the v0.21.6 sync it kept its
  own copies of `_load_prefill_messages`, `_resolve_prefill_messages_file`,
  `_parse_reasoning_config`, `_parse_service_tier_config`, `_mirror_config_to_env`,
  `_cli_config_defaults`, `_merge_file_config`, `load_cli_config`, the two assistant-copy
  helpers and three constants, defined after the imports of the same names. The copies won,
  so the CLI ran older code than upstream ships (no `utf-8-sig` config read, no `ultrafast`
  service tier, a hard-coded sandbox image). They are removed. The one fork change in them,
  `apply_managed_overlay(defaults, user_raw=file_config)`, moved to
  `hermes_cli/cli_config_load.py`. `cli.py` is upstream's file plus the required-skills
  preload.
- **`browser_exec` hybrid local sidecar** (upstream): a call that names a private URL runs on
  a local Chromium instead of the cloud provider. Managed mode ignores it: the leased slot is
  the only browser, and the harness daemon is never renamed to the sidecar's
  (`tools/browser_use_cli.py`).
- **Home Assistant plugin pin moved** (upstream `plugin-catalog/homeassistant.yaml`): the
  plugin now lives in `NousResearch/hermes-official-plugins` (subdir `homeassistant`,
  `682b22d019a1`, still v2.0.1). Code and tests are byte-identical to the previous pin; only
  `plugin.yaml` (homepage) and `README.md` changed. `VENDORED.json` records the new source,
  commit and hashes. The `adapter.py` cold-boot patch is unchanged.
- **Version**: `pyproject.toml` and `uv.lock` say `0.21.7`
  (`scripts/fork-release-version.sh --check` passes for `release/v0.21.7-dlz`).
- **New upstream CI surfaces**:
  - `remote_backends` lane (real sshd + Docker terminal backends) is kept and runs on
    standard runners; it is not in the `FORK_LEAN` off-list.
  - Every job must end with the `check-clean-worktree` step
    (`tests/ci/test_clean_worktree_check.py`); the fork-only jobs `tests.yml:plan` and
    `js-tests.yml:workspaces` got it.
  - E2E lanes are forced off unless the run is a release or a manual dispatch. `FORK_LEAN`
    still applies on top.
  - Docs-drift ratchet: `browser.control_url` (fork key, documented in monolith) is in
    `scripts/ci/docs_drift_baseline.json`.
- **Prompt-surface snapshot** (`tests/fixtures/prompt_surface/`, new upstream) is upstream's
  render. `scripts/ci/prompt_surface.py render` does not run on macOS
  (`_sysconfigdata__linux_darwin`), so the fork's render is still owed from a Linux host;
  until then `tests/ci/test_prompt_surface.py` fails wherever the fork's prompt or tool
  schemas differ from upstream's.

## Per-path hunk decisions

| Conflicted path | Resolution |
|---|---|
| `.github/actions/detect-changes/action.yml`, `scripts/ci/classify_changes.py` | both: fork selective outputs (`py_scope`, `py_roots`, `frontend_workspaces`, `os_tests`, `mode`) and upstream `remote_backends` |
| `.github/workflows/ci.yaml` | both: upstream `release` / `e2e_allowed` gate, then the fork's `desktop_e2e` and `FORK_LEAN` off-list |
| `.github/workflows/desktop-bundled-release.yml` | upstream `continue-on-error` on the Store job with the fork's repository-conditional runner |
| `.github/workflows/js-tests.yml` | fork matrix step kept; upstream clean-worktree step appended |
| `.github/workflows/lint.yml` | both: fork heredoc terminator, then upstream clean-worktree step |
| `agent/context_compressor.py` | fork `recent_token_budget` parameter with upstream's typing |
| `agent/secret_sources/onepassword.py` | fork `_OpError`, name blanking, `_run_op` and item grouping kept; upstream typing |
| `agent/vault_backends/onepassword.py` | fork `Login,Credit Card` listing and vault scope kept; upstream typing |
| `cli.py` | upstream `| None` signature plus the fork's `required_skills` parameter (see `cli.py` above for the later cleanup) |
| `gateway/run.py` | fork `_build_interim_media_delivery_payload` kept; upstream typing on `_interim_metadata` |
| `gateway/run_inbound.py` | fork per-clip pending STT cache kept; upstream typing |
| `hermes_cli/config.py`, `hermes_cli/config_effective.py`, `hermes_cli/managed_scope.py` | fork managed seed (user leaf wins, `user_raw`) kept; upstream typing and dropped `noqa` |
| `hermes_cli/kanban_pr_acceptance.py` | upstream f-string GraphQL query inside the fork's typed `_GhError` handling |
| `hermes_cli/write_approval_commands.py`, `tools/write_approval.py` | fork file kept (payload digest binding, durable staging); upstream typing only |
| `hermes_state_schema.py` | fork FTS fence kept (upstream changed one `noqa`) |
| `hermes_state_search.py` | fork `_like_boolean_groups` / `exact_tokens` kept; upstream typing |
| `tools/browser_use_cli.py` | both: fork managed route (`mode`, `identity`, `qa_targets`, vault route record) and upstream `force_local_sidecar`; see sidecar above |
| `tools/browser_vault_tool.py` | fork `_PageUnavailable` / `_managed_supervisor` kept; upstream typing |
| `tools/code_kernel.py` | fork `_record_cell_for_provenance` kept; upstream typing |
| `tools/mcp_tool.py` | upstream sorted `__slots__` plus the fork's `_auth_park_credentials` |
| `tools/memory_tool_store.py`, `tools/skill_manager_tool.py` | fork write guard, sha binding and author policy kept; upstream typing |
| `tests/ci/test_classify_changes.py` | both sides' cases; upstream's `remote_backends` cases carry the fork's selective expectations |
| `tests/ci/test_update_ci_routing.py` | upstream `event_name` replay on the fork's upstream-repository context |
| `tests/hermes_cli/test_left_core_migration.py` | both sides' cases (fork bundled-plugin, upstream Spotify) |
| `tests/hermes_cli/test_relay_shared_metrics_runtime.py`, `tests/tools/test_mcp_oauth_discovery_requests.py` | fork side |
