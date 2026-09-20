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

## Stable blocker identity

`kanban_db.block_task(..., blocker_key=...)`, `hermes kanban block --blocker-key`,
the `kanban_block` tool, and dashboard PATCH/bulk block payloads accept the same
optional nonblank key, at most 128 characters. Use a non-secret stable obstacle
identifier such as `source-packet-unreadable`, not a reason sentence or broad
category. Keys compare exactly (no trimming or case conversion). They are stored
on the task and blocking event, exposed in task readback, and retained on unblock.
Successful completion clears the key with the recurrence counter.

For keyed calls recurrence requires both kind and key equality; a new key starts
at one. An omitted key preserves historical kind-only counting, even after a
keyed call, and stores null (no fabricated key for legacy callers). Dependency
waits still bypass the breaker, and review changes do not touch cause accounting.
Do not rotate a key to evade repeated-failure escalation. Before image deployment,
live callers still use the old schema/accounting; no live upgrade is implied.

## Explicit operator recovery CLI

The default legacy API above still refuses `needs_input`. Ordinary `complete`
continues to refuse `triage`. Neither is a generic force operation. Two additional
operations live in `hermes_cli.kanban_db_operator`, with a CLI adapter in
`hermes_cli.kanban_operator`:

```bash
hermes kanban --board project operator-recover snapshot TASK_ID > snapshot.json
hermes kanban --board project operator-recover complete-gate TASK_ID --request gate.json
hermes kanban --board project operator-recover resolve-blocker TASK_ID --request resolution.json
```

The board must be explicit and its database must already exist. The CLI refuses
`HERMES_KANBAN_DB` overrides rather than silently targeting another board. Snapshot
output includes private task/history content: retain it appropriately. Request files
must be authored and reviewed by the trusted operator, not executed simply because
an agent or attachment supplied them. A request is one JSON object containing the
keyword arguments below; the task ID and board come only from the CLI positionals.
Unknown or missing arguments fail closed. There is no batch mode.

Both mutation APIs require:

| Field | Meaning |
| --- | --- |
| `expected_fingerprint` | Exact opaque fingerprint from the reviewed snapshot |
| `author`, `reason` | Nonblank operator identity label and explanation |
| `operator: true` | Explicit trusted-operator attestation |
| `process_quiescent: true` | Operator independently reconciled task/run/process ownership |

A fingerprint is not approval. Attestations are an **audit boundary, not same-UID
authentication**. Both mutations reject dispatcher worker and delegated-child
contexts. They are deliberately not forwarded by worker tools or the dashboard.
Same-UID code can alter its own environment or access SQLite directly; profiles do
not create security isolation. Do not remove worker markers to invoke these APIs.
No PID probing, killing, claiming, service restart, or deployment is performed.

### Complete a nonspawning evidence gate

`complete_triage_gate(conn, task_id, ...)` additionally requires:

```json
{
  "role": "operator-evidence-gate",
  "nonspawning": true,
  "summary": "What the independent evidence establishes",
  "metadata": {
    "evidence": {"reference": "operator-reviewed evidence location"}
  }
}
```

Merge these fields with the common fields above. `summary` must be nonblank and
`metadata.evidence` a nonempty object. Record actual evidence; the API does not
establish the truth of free-form assertions. No title, assignee name, lack of
children, or successful CI inferred from prose determines whether a task is a gate.
The operator must attest its nonspawning role and fulfillment of its requirements.

The transaction checks exact state, all parents done/archived, no claim/expiry/PID/
current-run pointer, and no open or running historical run. It performs **one direct
`triage -> done` write**, appends `operator_gate_completed` and native `completed`
events, and records a synthetic ended completion run carrying the handoff. There is
no ready/running window. Existing descendants, specification, comments and history
are preserved; native readiness may release descendants after the commit.

Native completion persistence also preserves declared managed-scratch artifacts
from `metadata.artifacts`, runs cleanup and completion hooks, and retains structured
handoffs. Missing managed-scratch artifacts abort completion. PR completion contracts
still use native independent acceptance collection, including exact-head required
checks; `metadata.published_pr` must match the contract. Network collection occurs
outside the writer transaction and the exact snapshot is checked again afterward.
A failed external acceptance leaves the gate in triage, records `pr_acceptance`,
and updates the native failure diagnostic; inspect a fresh snapshot before retrying.
As with ordinary completion, a repository contract binds once to a matching supplied
PR even when its checks fail. Retrying cannot substitute a green sibling PR. This
binding and receipt are recorded only after the snapshot recheck, in the writer
transaction. Attested evidence cannot bypass CI.

This operation does not discharge typed holds: a typed task blocker or latest typed
blocking event is refused. Goal-mode tasks are also refused rather than bypassing
their judge. Retain those gates for their existing decision/goal workflow; never
misdeclare a gate as executable merely to make it runnable.

### Resolve an exact typed hold on executable work

`resolve_triage_blocker(conn, task_id, ...)` additionally requires:

```json
{
  "role": "executable",
  "resolved_blocker": {"copy": "the entire exact non-null snapshot.blocker object"},
  "decision": "Explicit operator decision resolving only this named hold",
  "evidence_reference": "location of the actual decision or approval record"
}
```

The `resolved_blocker` example is a placeholder, **not a valid event**. Copy the
entire event from the snapshot, including its ID, serialized payload and run fields.
Decision and reference must be nonblank. The exact latest event, its typed kind and
blocker key must match the task as well as the fingerprint. Missing, mismatched,
stale and replayed resolutions are refused; another event with a similar reason
is not equivalent. This is resolution of an already accepted executable task, not
specification rewriting or authorization of new work.

Success records `typed_blocker_resolved` with the decision, evidence reference,
original blocker and before fingerprint, and leaves `todo`. The next native readiness
pass restores `ready` or `review` according to the saved source phase, only when
parents are satisfied. The reviewer assignment and descendants remain intact.
As in native unblock, dispatch-failure counters are reset, while original typed
cause and recurrence accounting survive. A fresh re-block can therefore escalate
again; resolution does not erase history or grant unlimited retries.

A technical `needs_input` reason never implies financial/human approval. The trusted
operator must reference the actual decision appropriate to that exact hold; this
operation neither grants actions nor clears unrelated holds or dependencies.

### Verification and bootstrap boundary

Behavioral contracts are authored in `tests/hermes_cli/test_kanban_operator_recovery.py`
(real synthetic SQLite and parser/CLI paths). They cover direct closure/no runnable
window, refusal and transaction rollback, acceptance races, artifact preservation,
exact-event resolution/replay, and parent-gated review restoration. These tests are
not runtime evidence until isolated exact-head CI executes them. Static AST/lint/diff
checks cannot establish behavioral PASS.

A new source/API cannot repair the image currently executing the old kernel.
Publication, exact-head CI, independent review, integration and operator-authorized
image deployment remain separate gates. Operators must reconcile the installed
version and task/process ownership, then obtain a **fresh installed-kernel snapshot**
before applying a reviewed request. Do not run a candidate checkout against the
production DB as a deployment substitute, patch SQLite, change titles to evade a
guard, or temporarily expose an operator gate to dispatch.

If the old image cannot close the evidence gate needed for its own upgrade, retain
that native hold. The deployment owner must first accept the candidate using the
independent external receipts and an explicitly authorized bootstrap deployment
procedure; only the upgraded, verified operator surface can close the native gate.
This change neither performs that bootstrap nor manufactures acceptance to unlock it.
