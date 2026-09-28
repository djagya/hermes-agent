# Focused-first fork sync preflight

`Hermes Sync Preflight` (`.github/workflows/sync-preflight.yml`) is the fork's
manual, diagnostic first stage. It neither calls full CI nor publishes anything.
Use a reviewed fork candidate; workflow dispatch executes that candidate's code.
Do not use this surface to execute an untrusted contributor ref with credentials.
Only read permission and non-persisted checkout credentials are used.

## Campaign inputs

Record these four immutable SHAs before dispatch:

- `candidate_sha`: the candidate head, including any post-merge corrections;
- `baseline_sha`: the fork head immediately before the sync;
- `upstream_sha`: the imported upstream head;
- `sync_merge_sha`: the two-parent merge, with baseline first and upstream second.

Dispatch **on the candidate branch/ref**, supplying its exact head as
`candidate_sha`. The dispatch SHA, checked-out SHA, candidate, and run receipt must
agree. A default-branch dispatch aimed at a different candidate is rejected, not
silently accepted as exact-head evidence. If the branch moves before dispatch,
refresh the inputs/review rather than accepting a receipt for another head.
For first introduction, ROOT must make the workflow discoverable on the default
branch (GitHub's dispatch requirement) before attempting a candidate-ref dispatch.
Rerun all jobs, not only failed jobs: attempt-bound receipts intentionally reject
a mixture of successful artifacts from an earlier attempt and new results.

The merge must lie on the candidate's first-parent spine. Provenance records:

- the sync tranche: `git rev-list --first-parent baseline..candidate`;
- the fork-side spine: `git rev-list --first-parent candidate --not upstream`.

Do not use `merge-base..candidate` as a list of fork-authored work: it includes
imported upstream history. The emitted spine describes fork-side provenance,
not personal authorship (cherry-picks retain contributor credit). Lists are
bounded to 200 commits and carry counts; the full fork-spine digest binds the
untruncated ordered list. Squashed/rebased syncs are intentionally rejected.

## Focused selectors

`selectors=all` runs the complete default tranche. Otherwise use an exact,
comma-separated subset (no spaces). Unknown/duplicate/empty selectors fail.

| Job/selector | Contract |
| --- | --- |
| `identity-and-contracts` | PM-generated Python lock consistency; npm lock dry-run; all-workflow actionlint syntax; existing fork runner-policy checker; existing compat import checker; installed test-environment dependency closure; real Python collection of migration and preflight contracts |
| `windows-paths` | Native Windows PowerShell installer and desktop updater predicates, Windows path behavior, UTF-8 BOM readers; import/collection and focused execution |
| `upgrade-config` | Config migration, sibling profile migration, unversioned config and already-current upgrade behavior; import/collection and focused execution |
| `sqlite-wal` | PM interpreter SQLite/WAL capability, reset predicate, active WAL and checkpoint strategy; import/collection and focused execution |
| `affected-python` | Import/collection and execution of `python_roots` (JSON array of test paths/globs; defaults to `tests/ci`) |
| `image-inventory` | Deterministic tracked image-source input inventory and non-executing `bash -n` smoke of image shell scripts |

Affected Python selection is explicit: it never silently expands to the full
suite. Every pattern must resolve, with 1–500 files in total. Docker and E2E roots
are rejected here. Use separate focused runs for larger shards. No `-k` or raw
shell input is accepted. The existing canonical test runner owns isolation and
file discovery/execution semantics; the existing `setup-pm` action owns pinned
Python/Node/tools, extras and the dev/test environment. There is no alternate
pip/venv bootstrap. File retries are disabled so an initial failure stays visible.

The image lane does **not** build, pull, start or publish an image. Its inventory
hashes the tracked Dockerfile, Docker sources, PM lock and dependency inputs.
It is explicitly labeled `image-source-inputs`, not a built-image inventory or
runtime smoke pass. Actual image filesystem inventory, metadata/budget gates and
Docker/s6 integration remain owned by `fork-release-image.yml` in the later
acceptance stage. This avoids spending an image build just to discover cheap
source/lock/collection failures. No preflight command calls live s6 or accesses
`/run/service`.

## Failure manifest and attribution

The always-run `failure-manifest` job combines per-lane receipts into one
`sync-preflight-manifest` artifact:

- `failure-manifest.json`: schema version, exact candidate, run ID/attempt,
  job/check identities, outcomes, command/log references, causal classes and
  the explicit `full_acceptance: not run` boundary;
- `summary.md`: bounded human summary, also copied to the Actions run summary.

Per-lane artifacts retain diagnostic logs and the image-source inventory.
Commands continue after an independent failure; matrix fail-fast is disabled.
Lock/syntax/policy diagnostics are persisted before test-dependency installation,
so a broken lock cannot hide its own failure behind a bootstrap error.
Missing, foreign-head, wrong-attempt, malformed or interrupted receipts fail the
aggregate gate. GitHub dependency-job results are checked too, so a post-action
failure cannot be hidden by an earlier successful receipt. Missing bootstrap/runner
evidence is not a green skipped lane.
Only selected lanes count toward this diagnostic gate; a subset cannot establish
whole-tranche acceptance. Failed checks make the final gate red even when the
collection job successfully finished gathering diagnostics.

The manifest reserves these causal classes:

| Class | Evidence required |
| --- | --- |
| `baseline fork debt` | Same failure reproduced on the named pre-sync fork baseline under a compatible environment |
| `inherited upstream behavior` | Same failure reproduced on the named upstream head; baseline comparison excludes pre-existing fork debt |
| `merge-only regression` | Failure on candidate with both parent comparisons passing the same contract |
| `fork-policy incompatibility` | A named fork contract rejects the candidate (for example the existing larger-runner guard) |
| `infrastructure/runner failure` | Command could not start, command timeout, or missing/invalid/incomplete job receipt |
| `unresolved` | Any failure not justified by the evidence above |

V1 does not automatically run both parents or guess causal ancestry from a
traceback, touched filename or commit author. Test/lock/syntax failures start as
`unresolved`; ROOT obtains focused parent receipts before attributing them to
baseline, upstream or merge. Record those immutable run/head/check receipts in
the campaign's acceptance record, alongside the original manifest; do not
rewrite the original observed result. A timeout is an infrastructure observation,
not proof that the implementation is innocent. Actionlint intentionally includes
upstream-owned workflows: existing syntax debt must be visible, not filtered out.

Each receipt is capped at 1 MiB on ingestion and 200 check records. Diagnostic
logs retain at most the last 128 KiB each, explicitly marked when truncated;
they are not an exhaustive traceback archive. The manifest references logs
rather than embedding arbitrary output. Job identity is the logical lane plus
run ID/attempt; use the Actions run's job list to resolve the platform's numeric
job ID. If GitHub cancels or fails to provision even the aggregation job, there
is no acceptance receipt: ROOT must treat the absent manifest as incomplete.

## Acceptance sequence / handoff template

1. Commit and cold-review one candidate, preserving the merge ancestry.
2. Dispatch `Hermes Sync Preflight` on that exact candidate, initially `all`.
3. Read the complete manifest, not just the first red job. Classify unresolved
   failures with focused exact-parent receipts; fix the causal classes in scope.
4. Any candidate change invalidates candidate test receipts. Repeat the affected
   lanes, then obtain a complete default-tranche receipt on the frozen head.
5. Explicitly dispatch the later full acceptance gates: `CI / All required checks
   pass`, Termux, PM Bundle, Windows bundle SDK, and fork image build plus
   Docker/s6 integration. Preflight green is not a substitute for any of them.
6. ROOT owns publication, integration and final exact-head receipt acceptance.

Campaign handoff fields:

```
candidate / baseline / upstream / sync_merge:
preflight run URL + attempt:
selected lanes + manifest artifact:
failures by causal class (unresolved stays explicit):
parent comparison receipts, if any:
full acceptance receipts (or NOT RUN):
next owner/action:
```

Hermes CI-only execution policy applies: on the live worker host, edit/read/diff
and static analysis only. Do not run this helper's execution modes, Hermes
collection/tests or Docker lifecycle probes there. New behavior-contract tests
are in `tests/ci/test_sync_preflight.py`; run them through `scripts/run_tests.sh`
on the exact-head isolated CI surface, not locally.
