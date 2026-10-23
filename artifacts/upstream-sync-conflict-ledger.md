# Upstream merge conflict ledger (sync/v0.22.5 candidate)

Base HEAD: `49ae702278d6b321f8d9cffb959fbfc2122a1732` (release/v0.22.2-dlz, the pinned image)
Upstream MERGE_HEAD: `bf0203a794a07aac3ff764972ab9087baa4e4f4e` (tag `v0.22.5`)
Merge base: `d923a5eb487a17c0a68801bba9ef0f56c81040a8` (tag `v0.22.2`, the previous sync)

Same method as the v0.22.2 sync: one two-parent merge of the upstream release onto the fork
line, conflicts resolved hunk by hunk, everything outside a conflict taken from both histories.
46 paths conflicted (all content). Sizing from the tree: v0.22.5 is a commit on upstream
`main` (no wrong-commit tag this time), 3291 commits / 4690 files past v0.22.2; v0.22.3 and
v0.22.4 are ancestors. The fork carries 472 changed paths over v0.22.2; 132 of them overlap.

Schema moves in this release: config `_config_version` 50 -> 51 (drops
`cron.bot_chat_delivery_timeout_seconds`), state `SCHEMA_VERSION` 31 -> 32 (no-op data step,
#140315). The `tools.tool_search.defer` default list is unchanged since v0.22.2.

## Decisions that change behavior (operator review)

- **Kanban workers run inside the assignee's profile owner** (upstream). The dispatcher no
  longer spawns `hermes -p <profile> --cli chat -q`; it spawns `hermes_cli.kanban_worker_client`,
  which submits the claim to the assignee profile's running gateway (`kanban.run`), and the
  turn runs as a managed worker there (`gateway/session_kanban.py`). Every assignee profile
  needs a live owner: its own gateway or a multiplexer that serves it
  (`gateway.multiplex_profiles`). Cron Bot Chat deliveries move the same way (config 51).
- **Mandatory card skills** (fork `--required-skills`): on the owner path they are unioned
  into the card's skill list, so they load. The fork's hard refusal (typed exit when a
  mandatory skill is missing) is not ported to the owner path yet. Open item.
- **Fork CI stays on GitHub-hosted runners.** Upstream moved CI to Blacksmith between v0.22.2
  and v0.22.4. The fork has no Blacksmith app; labels and actions are mapped back (separate
  commit), and the runner guard now refuses an unguarded `blacksmith-*` label.
- **Debian base**: upstream pins `debian:trixie-20261020`; the fork's slim `runtime` stage
  follows it (same libc as `sqlite_build`), with the fork's snapshot ARG unchanged.
- **CPython 3.14.9** (PBS 20261021), kept on the fork's `install_only_stripped` flavor; the
  eight artifacts were re-pinned from the release's `SHA256SUMS`.
- **Pillow 13.0.0** arrives from upstream (override past browser-harness's 12.3.0 pin), so the
  Pillow 12.3.0 Trivy ignores become stale after this image ships.
- **Version**: `pyproject.toml` and `uv.lock` say `0.22.5`; `uv.lock` is upstream's v0.22.5
  lock re-resolved with uv 0.13.5 (the PM pin); fork `httpx2`/`httpcore2` 2.13.0 kept with
  upstream `mcp` 2.3.0.

## Per-path hunk decisions

- .github/** runner hunks: fork side (GitHub-hosted / guarded labels). See CI section.
- js-tests.yml: fork per-workspace matrix kept; added upstream's "Prepare locked Electron runtime" step (Electron 42 no longer downloads in postinstall) for the apps/desktop matrix rows only.
- setup-pm/action.yml: fork restore/save split kept (already excludes __pycache__, saves only on trusted refs); upstream PR-default save inputs merged around it.
- agent/context_compressor.py: both imports (fork skill-recency + upstream ProactivePruneMixin).
- agent/session_persistence.py: upstream moved sanitize_context to agent.memory_context; fork vault redaction import kept.
- hermes_cli/cli_commands_mixin.py: both (fork managed_connect_refusal + upstream _normalize_cdp_url import from cli_browser_gateway).
- gateway/run_busy.py: upstream split the interrupt+ack tail into _busy_interrupt_and_ack (shared with the session authority's busy preempt in run_inbound). Fork's pre-queue supersession stamp stays before queueing; inside _busy_interrupt_and_ack the interrupt predicate is the (idempotent) _stamp_busy_interrupt_supersession, so the preempt path stamps too.
- gateway/run_inbound.py + run_inbound_transcription.py: upstream moved the STT helpers into GatewayInboundTranscriptionMixin and independently added a per-clip memo (_gateway_pending_stt_clips) for voice notes merged during STT. Fork keeps its PendingAudioClip design (shared per-clip tasks, echo claimed by clip identity, background _start_pending_voice_stt): the fork's _transcribe_pending_audio_event_once / _echo_pending_stt_transcripts_once / _transcribe_and_echo_pending_voice / _start_pending_voice_stt replace upstream's in the new mixin; upstream's _enrich_message_with_transcription(clip_memo=None) kept (fork calls it without a memo).
- gateway/run_turn.py: upstream moved the turn-persistence/delivery helpers (_hmwa_shape_agent_response … _hmwa_discard_stale_result) into gateway/run_turn_persistence.py and _hmwa_prepare_turn into run_turn_prepare.py. Each fork-modified method was 3-way merged into its new home (only _hmwa_deliver_turn_response differed; clean merge); fork-only _hmwa_final_assistant_row_id and _hmwa_stamp_delivery_target were added to the persistence mixin. The call site keeps the fork's _hmwa_stamp_delivery_target before upstream's new _deliver_turn_response_then_limit_offer.
- gateway/run_turn_runner.py: both (fork interim_media_responses capture + upstream local-viewer message.interim publish; fork interim_media_responses result key + upstream _compressed/current_turn_user_idx keys).
- hermes_state_messages.py: upstream moved set_latest_user_api_content/set_message_api_content to hermes_state_api_content.py; fork-only set_message_delivery stays in hermes_state_messages.py.
- hermes_state_schema.py: upstream moved the open-time FTS stamp into _stamp_settled_fts_layout (fts_layout_current) and bumped SCHEMA_VERSION to 32 (no-op data step, #140315). The fork's guard (never stamp an older raw-content messages_fts aligned) is added to _stamp_settled_fts_layout.
- hermes_cli/kanban_db_dispatch.py _worker_argv: BEHAVIOR CHANGE (upstream). Kanban workers no longer run `hermes -p <profile> --cli chat -q`; the dispatcher spawns hermes_cli.kanban_worker_client, which submits the claim to the assignee profile's running owner (gateway) via kanban.run and the owner runs the turn (gateway/session_kanban.py). Upstream wins. Fork --required-skills: required skills are now unioned into the owner-path skill list (gateway/session_kanban.py), so they load; the fork's hard refusal when a mandatory skill is missing (typed exit) is NOT ported yet -- open item. Fork _resolve_worker_cli_toolsets (explicit --toolsets pin) is no longer used by the launch path.

- `agent/vault_backends/onepassword.py`: both sides fixed service-account item reads needing
  `--vault`. Fork kept (vault-qualified handles, card listing and `resolve_secret`); upstream's
  `_item_vaults` listing map is still populated by its non-conflicting lines but unused.
- `tools/browser_tool_cdp.py`: both (fork `urlsplit` import, `_is_managed_browser` and its
  managed short-circuit before upstream's per-profile `live_cdp_override() or env_cdp_url()`).
- `tools/browser_camofox.py`: fork managed-mode refusal first, then upstream's per-profile CDP
  override check.
- `tools/browser_use_cli.py`: fork managed routing (`mode`/`identity`/`qa_targets`, leased slot,
  `local=true` refusal, permission-blocked re-route) kept; upstream's real-profile fallback
  (`real_profile`, `_route_exec_current`, `_reload_if_republished`) added, with the fork route
  keywords threaded through `_route_exec`/`_route_exec_current`.
- `tools/approval.py`: both (fork `_undefined_name_provenance`, upstream `_is_local_viewer_turn`).
- `plugins/platforms/telegram/adapter.py`: both mixins (fork inline photos, upstream topic routing).
- `Dockerfile`: upstream base digest + fork `DEBIAN_SNAPSHOT` ARG; fork runtime stage moved to the
  same base.
- `pyproject.toml`: upstream urllib3 2.8.0 / PyJWT 2.15.1 / mcp 2.3.0; fork httpx2 2.13.0;
  fork aiohttp comments (same version); version 0.22.5.
- `uv.lock`: upstream's v0.22.5 file, then `uv lock` (uv 0.13.5).
- `pm/lock.json`: upstream CPython 3.14.9, restamped to `install_only_stripped`.
- `package.json`, `apps/desktop/package.json`, `package-lock.json`: upstream versions (dompurify
  3.4.17, ip-address 10.7.4, nanoid 3.3.20, katex override) plus the fork's `browserslist` 4.28.7 /
  `baseline-browser-mapping` 2.11.0 overrides and their lock entries.
- `plugins/platforms/photon/sidecar/package-lock.json`: upstream (grpc-js 1.14.6, undici).
- Tests: `test_classify_changes.py` both kwargs (`installer`, `catalog`);
  `test_api_server_active_work_drain.py` and `test_hosted_rooms.py` upstream (same fixes);
  `test_mcp_oauth_manager.py` both; `test_fts_trigram_cron_exclusion.py` fork skip marker on
  upstream's parametrized signature.
- `tests/fixtures/prompt_surface/**`: upstream, then re-rendered for the fork with
  `scripts/fork-prompt-surface.sh` (monolith) before the gate.
