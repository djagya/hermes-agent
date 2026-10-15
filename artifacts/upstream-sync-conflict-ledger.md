# Upstream merge conflict ledger (sync/v0.22.2 candidate)

Base HEAD: `6cdb34e7aced42e996749604a5c191b9806b2940` (release/v0.21.7-dlz, the pinned image)
Upstream MERGE_HEAD: `d923a5eb487a17c0a68801bba9ef0f56c81040a8` (tag `v0.22.2`)
Merge base: `71d34bad1045610252eda02ee79549436896e784` (tag `v0.21.7`, the previous sync)

Same method as the v0.21.7 sync: one two-parent merge of the upstream release onto the fork
line, conflicts resolved hunk by hunk, everything outside a conflict taken from both histories.
29 paths conflicted (27 content, 2 modify/delete). The previous ledger (sync onto `71d34bad10`)
is in git history at release/v0.21.7-dlz.

Unchanged by this release: config schema (`_config_version` 50,
`hermes_cli/config_defaults.py`) and state schema (`SCHEMA_VERSION` 31,
`hermes_state_common.py`).

## Decisions that change behavior (operator review)

- **The five vault tools are one tool** (upstream `3bf97898ac`): `browser_vault_list`,
  `_fill`, `_unlock`, `_save_login` and `_enter_code` became `browser_vault` with an `action`
  enum (`list`, `fill`, `save_login`, `enter_code`, `unlock`), deferred behind `tool_search`
  (only its catalog line is paid per request; the model reaches it through `tool_call`). The
  fork takes the folded tool and keeps its own parameters on it: `merchant_origin` for card
  fill, and `code` + `expected_origin` for a code Hermes obtained through its own mail workflow.
  The description carries the fork's card-fill rules (bound origins, the selected-merchant
  1Password card, one card document incl. a Stripe hosted iframe, no seller-reputation claim,
  never submits). Skills, docs or receipts that name the old tools must switch to
  `browser_vault action=...`.
- **Supplied one-time codes stay redacted on the folded tool.** The fork's early OTP
  registration (`agent/tool_executor.py`) and preview mask (`agent/display.py`) were keyed on
  the `browser_vault_enter_code` name. They now match `browser_vault` (a `code` on any action)
  and keep the old name for older transcripts and staged replays.
- **Memory `replace` is gone from the tool; `patch` edits one span** (upstream). The fork's
  staged-write guard (`expected_sha256`, `before_write`) is threaded through `patch` /
  `_patch` like the other store mutations, and `_batch` keeps the guard plus upstream's
  top-level target check. `replace` stays in `_STORE_ACTIONS` for replaying older staged writes.
- **Parked OAuth MCP servers**: upstream and the fork fixed the same replay of a dead refresh
  token in two places. Both stay, armed by one `_arm_auth_park`: upstream's `_auth_park`
  (token storage + credential stamp) stops discovery passes from nudging the server, and the
  fork's `_auth_park_credentials` (credential stamp + endpoint/auth config) keeps the timed
  self-probe asleep until a re-authorization or config edit.
- **Kanban active-PR respawn guard** keeps the fork's rule (only the worker's own PR comment
  counts; one explicit unblock after it allows one claim). Upstream's v0.22.2 change there was
  value comparison of mixed integer/ISO timestamps; the fork guard now filters the window and
  orders the legacy unblock fallback by parsed epoch, failing closed on an unreadable time.
- **Home Assistant plugin pin moved** to hermes-official-plugins `a4e311e433` (still v2.0.1):
  a `HASS_URL` without a scheme is a non-retryable fatal error, and the standalone (cron) send
  calls `notify.<target>` first, falling back to the grouped `notify.notify` only on a 400 or an
  empty target. Re-vendored; the fork's cold-boot grace patch on `adapter.py` is unchanged.
- **uv quarantine**: upstream now sets `exclude-newer = "3 days"` without per-package
  exceptions; the fork follows. `httpx2`/`httpcore2` stay at the fork's 2.13.0 (upstream
  2.12.0); `uv.lock` was re-resolved with uv 0.12.3 (the PM pin).
- **npm**: upstream's `undici` 6.29.0 / 7.30.0 and `yaml` 2.9.1 win; the fork's
  `browserslist` 4.28.7 / `baseline-browser-mapping` 2.11.0 overrides stay (the lock already
  resolves those versions).
- **Version**: `pyproject.toml` and `uv.lock` say `0.22.2`
  (`scripts/fork-release-version.sh --check` for `release/v0.22.2-dlz`).
- **CI commit threading** (upstream): reusable workflows take a `commit` input that every
  checkout follows (`tests/ci/test_stable_release_commit_threading.py`). The fork-only
  `js-tests.yml:workspaces` and `tests.yml:plan` checkouts follow it too; the fork's
  `tests.yml` N-1 job keeps its PR-head fallback after `inputs.commit`.
- **Prompt surface** is re-rendered for the fork's schemas with
  `scripts/fork-prompt-surface.sh` (monolith) after these commits.

## Per-path hunk decisions

| Conflicted path | Resolution |
|---|---|
| `.github/workflows/ci.yaml` | both: fork selective inputs (`scope`, `roots`, `workspaces`) and upstream `commit` |
| `.github/workflows/js-tests.yml`, `.github/workflows/tests.yml` (inputs) | both inputs (fork `workspaces` / `scope` / `roots`, upstream `commit`) |
| `.github/workflows/tests.yml` (N-1 checkout) | `inputs.commit`, else the fork's PR head / `github.sha` |
| `.github/workflows/lint.yml` | upstream `ref: inputs.commit` with the fork's `fetch-depth: 0` (health ratchet) |
| `apps/desktop/package.json`, `ui-tui/package.json`, `package-lock.json` | upstream |
| `package.json` | upstream overrides plus the fork's `browserslist` / `baseline-browser-mapping` |
| `pyproject.toml` | fork `httpx2==2.13.0` (3 extras) |
| `uv.lock` | upstream, then re-resolved (`uv lock`, uv 0.12.3) |
| `hermes_cli/kanban_db.py` | upstream column lists (`last_heartbeat_at` is an epoch column now) plus fork `blocker_key` |
| `hermes_cli/kanban_db_dispatch.py` | fork guard with epoch-safe time handling (see above) |
| `hermes_cli/write_approval_commands.py` | fork `applied_result` plus upstream `patched` report line |
| `model_tools.py` | upstream vault note (tool_call, action=list) plus the fork's enter_code mail-code sentence |
| `tools/approval_smart.py` | fork guardian observation/parse helpers and `provenance` parameter plus upstream `_verdict_reasoning` |
| `tools/browser_vault_tool.py` | upstream folded tool with the fork's parameters and card-fill text (see above) |
| `tools/mcp_oauth.py`, `tools/mcp_tool.py` | both (`has_durable_tokens` + `credential_stamp`; both park slots) |
| `tools/memory_tool.py`, `tools/memory_tool_store.py` | upstream `patch` with the fork's write guard (see above) |
| `tests/ci/desktop_release_roles.py` | both expression helpers (`startsWith`, `fromJSON`) |
| `tests/hermes_cli/test_left_core_migration.py` | both sides' cases |
| `tests/fixtures/prompt_surface/**` | upstream, then re-rendered for the fork; `browser_vault_fill.json` / `browser_vault_enter_code.json` deleted with the folded tool |

Outside conflicts: `tools/mcp_tool_server_run.py` (single `_arm_auth_park`), `agent/tool_executor.py`
and `agent/display.py` (OTP redaction), fork vault tests and the credential-vault doc moved to
`browser_vault action=...`.
