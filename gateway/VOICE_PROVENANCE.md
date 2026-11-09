# Inbound native-voice provenance

Gateway user-message persistence carries `display_metadata.inbound_voice` for native
`voice` events with a platform message ID and absolute local media paths:

```json
{"inbound_voice":{"schema":1,"platform_message_id":"fixture-message","audio_paths":["/example/cache/voice.ogg"]}}
```

The adapter-provided event is the source, not transcription text, model prose,
filename matching, cache searches, or a guessed path. Ordinary agent turns,
queued follow-up turns, and failed-turn transcript rows all retain this binding.
Existing owner, reply-expectation, and diagnostic metadata remain independent.
Audio documents, missing IDs, empty media, relative paths, and remote URLs do
not acquire a binding. Duplicate identical paths collapse in arrival order.

Multiple distinct paths remain a list. There is no per-clip message-ID mapping:
a consumer requiring one clip must reject multiple paths rather than select the
first one. This change neither resolves ambiguous clips nor backfills old rows.

This metadata is provenance within the adapter trust envelope, not a security
boundary or proof of file existence, retention, or content. Consumers must apply
their own access checks and retention/copy policy. Absolute local paths are private
runtime metadata: do not publish production rows or logs as test fixtures.

## Integration and acceptance

1. Review the scoped commit and confirm its parent is the intended `main` base.
   If the base has moved, transplant only this change, inspect conflicts, and
   repeat the tests on the resulting head. Do not move release refs.
2. Run focused tests in the repository's test environment, with optional gateway
   dependencies available:
   `scripts/run_tests.sh tests/gateway/test_inbound_voice_provenance.py`.
   Also run the existing gateway persistence and silence-token regressions.
   On a live supervised host, use the operator's `hermes-pytest` guard instead;
   never run Docker or live supervisor lifecycle tests there.
3. Record external CI URL, exact head SHA, and executed test targets. Local guarded
   tests are exercised evidence only, not an external CI receipt.
4. Publication, CI dispatch, and deployment require separate operator authority.
   This candidate does not install plugins, reload modules, or restart services.
5. After an authorized rollout, send one synthetic native voice message. Inspect
   the persisted user row: its binding must contain the same event ID and downloaded
   media path, alongside existing metadata. Repeat as a queued follow-up and as a
   failed turn. Test an audio document as a negative control. Keep real audio out
   of public artifacts.
6. Validate each downstream consumer separately. A producer rollout alone does not
   prove forwarding or durable media copying. Old rows need a separately authorized
   backfill; do not infer missing bindings from model output.

Rollback the source commit through the normal reviewed release procedure. Existing
metadata is additive and may remain in historical rows; rollback does not remove
already copied consumer artifacts. Never repair a deployment by editing live code.
