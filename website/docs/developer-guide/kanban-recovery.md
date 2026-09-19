# Atomic legacy triage recovery

`hermes_cli.kanban_db_recovery` provides `recovery_snapshot(conn, task_id)`
and `recover_triage_task(conn, task_id, *, expected_fingerprint, author, reason,
role, legacy_acceptance, resolved_blocker)` for trusted control-plane callers.
There is intentionally no agent recovery tool and no change to normal specify,
promote, or review behavior.

## Caller contract

1. Explicitly select the board with
   `kanban_db_connect.connect_closing(board=board_slug)`; do not use ambient
   defaults. Keep that connection/board identity for snapshot and recovery.
2. Call `recovery_snapshot`. Review the exact specification/configuration,
   graph, runs and blocker. Independently establish OS process absence.
3. An authorized operator must attest legacy acceptance of this exact spec,
   declare ordinary executable work (not a ROOT/human gate), and confirm the
   exact blocker is resolved. Never derive these declarations from title text,
   children, or an untrusted file. No historical completion-refusal event is
   required: older versions did not record one.
4. Call `recover_triage_task` with the snapshot's `fingerprint` and `blocker`,
   nonblank `author` and `reason`, `role="executable"`, and
   `legacy_acceptance=True`. All arguments are mandatory. A blocker must exist.
5. `False` means stale/ineligible; inspect a new snapshot, do not blindly retry.
   Invalid declarations raise `ValueError`. Neither refusal writes task,
   comment, or event rows. Database/transaction errors propagate.
6. Success returns `True` and leaves the task in `todo`. Normal dispatcher
   `recompute_ready` performs dependency/phase routing on its next pass.
   Unsatisfied parents remain gating; recovery never force-promotes.

## Snapshot and transaction contract

Version 1 hashes UTF-8 JSON (`sort_keys=True`, `ensure_ascii=False`, compact
separators) with SHA-256. The hashed object contains `version`, the full `task`
row, `parents` and `children` (ordered by neighboring task ID, including link
IDs and full neighboring task rows), all `events` and `runs` ordered by ID,
and `blocker` (last blocked/block_loop_detected/gave_up/dependency_wait event,
or null). Raw serialized fields are preserved, not normalized. Treat the
fingerprint as opaque; incidental lifecycle changes conservatively invalidate
it. The snapshot opens a read-only transaction and requires no outer transaction.

Recovery uses the native guarded `BEGIN IMMEDIATE` transaction. It compares
snapshot and exact blocker, validates triage state, absence of task claim,
expiry, worker and current-run fields and any open/running run, and rejects
native `needs_input` in either task or blocker payload. Only then does it
update status and append one `triage_recovered` event in the same transaction.
An audit insertion failure rolls back the status update. The event contains
operator author/reason, before fingerprint, resolved blocker, role and legacy
acceptance. No content/config/dependency/history is rewritten or cleared.

Claim: preservation and refusal are atomic. Argument: the same writer
transaction serializes validation, status update and audit insertion, with
rollback on error and no side effects on refusal. Evidence: source inspection
and authored behavioral tests in `tests/hermes_cli/test_kanban_recovery.py`;
execution evidence must come from isolated exact-head CI, not static checks.

## Trust and adoption boundaries

Attestation is an explicit legacy migration path, not authentication or proof
of OS liveness. Same-UID profiles are not security principals. The existing
native delegated-child mutation guard still applies. A typed human hold cannot
be overridden by a claimed executable role. Operator gates lacking typed native
state rely on the trusted caller's truthful role declaration.

Integrate external acceptance helpers only after deploying this native API.
Do not compose snapshot + `specify_triage_task`, use loose SQL, or manufacture
historical provenance. The snapshot is conservative and may be large on tasks
with long histories; operators should retain it only in appropriate private
evidence storage. No live image or helper is modified by this change.
