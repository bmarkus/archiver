# Operation workspace API (Plan 005.3)

A sync workspace is a temporary SQLite manifest for one attempt. It stores
proposed actions and progress, never a second permanent inventory. A Catalog
remains authoritative. **005.3 does not implement copying, deletion, collision
naming or a sync executor.** 005.4 will build and execute production manifests.

Run `uv run python examples/operation_workspace.py` for an example that constructs
one synthetic proposed copy, inspects it, reopens the workspace and discards it.
All example data lives in a temporary directory; no copy is performed.
For a slower walkthrough with short explanations and small tables, open the
[Plan 005.3 notebook](../notebooks/public/plan-005.3-operation-workspaces.ipynb).

Import `SyncWorkspace`, `ManifestEntry`, `OperationScope`, `OperationSpec`,
`OperationCounts`, `OperationRecord`, `ManagedRootAuthority`, `PlacementEvidence`,
`PlacementIntent`, `PlacementCheck`, `FinalizationRequest`, `OperationError` and `recover_placement`
from `archiver`. Records are frozen dataclasses. Exceptions from filesystem/SQLite
validation also retain their standard types; invalid bounds use `ValueError`.

## Constructing and inspecting storage

Use `catalog.operation_scope(root)` to capture each endpoint, then
`OperationSpec.sync(source_scope, target_scope, keep_last=2)` for a fresh operation
UUID. Source/target roots must be accessible, physically disjoint directories with
no symlink/junction ancestors. Distinct roots in the same Catalog are supported.
UUIDs identify catalogs; paths and opaque revisions also scope reopening and
publication. As with comparison, checks cannot prevent every concurrent database
file replacement. The default retention is two successful target snapshots;
`None` retains all. Scanning defaults remain unchanged.

`SyncWorkspace.create(parent, spec, *, expected_count, expected_bytes)` creates
`parent/archiver-<operation UUID>/workspace.sqlite3` exclusively and returns the
handle with its absolute `directory` and immutable `spec`. Parent must exist and
be outside both content roots. Totals cover all classified source identities,
including represented skips; bytes count each source identity once. They are
not the comparison summary's missing-only `bytes_to_copy` when skips are included.

The independent workspace format is version 1. Foreign keys, rollback journaling
and full SQLite synchronization are enabled. Commits rely on the filesystem's
durability. The format is strict: unsupported/corrupt workspaces are rejected
before writable takeover. No automatic conversion or rebuilding occurs.

`add(entries, batch_size=1000)` accepts one bounded batch. Each `ManifestEntry`
contains full `content_id`, size, source-relative POSIX path, cached mtime and
optional proposed target-relative POSIX path. Initial copy entries are `pending`;
represented content uses `action='skip_present', state='skipped'` with no target
path. `register_existing_verified` is reserved for explicit recovery with a
predecessor reference and placement evidence. Duplicate identities, path aliases,
file/descendant destination collisions, escaping paths and control artifacts are
rejected transactionally. Source duplicates produce one manifest identity.

`batches(batch_size=1000)` yields immutable tuples in full algorithm/digest order.
Close its generator after an early break. `summary()` returns complete SQL counts,
independent of page size: planned count/bytes, skipped, placed count/bytes,
conflicts, failed, unattempted and registered count/bytes. Workspace counts describe
progress; registered counts are established by the frozen request and matching
Catalog receipt, not by saved `placed` flags.

Bounds are positive SQLite integers, excluding bool. Individual insert/handoff
batches cap at 10,000 rows and reject overflow; reads return at most 10,000 even
if a larger maximum is requested. Storage/sorting may grow with total manifest
size, but Python memory depends on a batch. There is no whole-manifest fetch API.

`mark_ready(comparison_exhausted=True)` requires complete matching totals and
resolved unique destinations. Production comparison consumption, collision naming
and destination assignment remain 005.4. An interrupted build cannot be resumed;
inspect/discard it and create a new operation.

## Ownership and lifetime

`close()` and context exit release the connection and owned controller lease;
they never delete the workspace. `SyncWorkspace.open(directory)` defaults to
inspection-only and changes no state. `writable=True` requires an exclusive
controller lease. After an unclean exit, `takeover=True` is explicit attestation
that the previous controller has stopped; PID/timeout guesses never grant access.
Taking over active execution/finalization records `recovery_required`.

`begin(source, target, authority=ManagedRootAuthority(operation_id, root, 'sync'))`
validates both captured baselines and authority, then records a target operation
start and workspace execution phase. It does not copy anything. Authority is
caller-supplied for the current handle and is never restored from the database.
The local dedup contract uses an `OperationSpec` of kind `dedup` with no source;
it needs no sync workspace or destination. Its executor belongs to 005.5.

## Progress and recovery interfaces for future executors

These storage/protocol interfaces are intended for a future executor or synthetic
tests, not a substitute for fresh filesystem verification:

- `record_outcome(entry_id, state, evidence=None, error_code=None, error_detail=None)`
  enforces `pending -> copying -> verified -> placed`, with explicit failure or
  conflict exits. Entering `copying` commits a `PlacementIntent` with an owned
  staging directory, deterministic witness name and destination before file creation.
  Verified/placed records require evidence matching that saved intent.
- `freeze()` atomically captures selected destination observations and complete
  final counts as an immutable, digested request, entering `finalizing`. It does
  not publish. No uncertain copying/verifying entries may be frozen.
- `require_recovery()` records interruption. `recover_entry(entry_id, check)`
  evaluates freshly supplied `PlacementCheck` facts; saved success is insufficient.
- `resume(source, target, authority=..., checks=...)` checks the receipt first,
  then unchanged baselines and all required fresh per-entry checks. A valid
  unfrozen attempt returns to execution; a frozen one retries the identical request.
  Changed frozen entries or baselines require explicit replacement planning.
- `acknowledge(target)` resolves a matching receipt without rechecking old
  baselines or publishing again. Even a pruned original snapshot cannot erase
  that receipt. Acknowledgement never reinterprets a later filesystem change as
  failure of the original commit.
- `fence(target, superseded=True)` serializes receipt lookup versus abandonment
  with finalization. If uncommitted, it prevents old-ID publication. A successor
  spec names `predecessor_id` and has a new UUID and fresh captured revisions.
- `handoff(successor, entry_ids, batch_size=1000)` durably transfers bounded
  placement evidence with retryable acknowledgements and original target paths.
  005.4 must freshly verify source eligibility and existing destinations before
  constructing executable replacement plans. The predecessor retains witnesses
  until the successor resolves them; unknown/unrequested files require explicit
  targeted reconciliation, never silent adoption or another copy.

The `retained_link_v1` contract records an owned target-filesystem staging directory,
witness path, file identifier, expected bytes and destination intent. 005.4 will
copy/verify a staging file, retain it as a witness and place by no-replace hard-link
creation. Recovery requires that witness and destination to refer to the same
physical file plus a fresh expected SHA-256 match. Metadata identifiers prove
placement association, not content identity. Unsupported filesystems must fail
before copying; 005.3 does not implement these filesystem actions or capability
checks. Managed roots must be quiescent; SQLite cannot roll back physical changes
or eliminate arbitrary external filesystem races.

The private Catalog finalization hook currently accepts small prepared database
change sets. It owns one transaction for inventory, retention and the terminal
summary/receipt. It rejects stale target revisions and caller transactions.
005.4 must add bounded successful-entry ingestion, not pass an entire large
manifest to `prepare_file_update()`. Empty successful sets create no snapshot
and prune nothing. No operation rows or tags are written to a distinct source
Catalog. With shared-Catalog roots, bookkeeping shares the database but leaves
source inventory and tags unchanged.

## Results and disposal

`completed` includes empty/all-skip successful attempts; `partial` includes mixed
successful and unsuccessful outcomes; a finalized all-unsuccessful attempt is
`failed` with a zero-registration receipt. Unknown physical or commit outcomes
require recovery instead of a terminal success status.

`dispose(target)` requires a matching receipt. It removes only exact owned workspace
artifacts after physical outcomes and owned in-root artifacts are resolved,
including for finalized all-failed attempts. With saved placement evidence the
future executor must resolve its artifacts and attest `artifacts_resolved=True`;
disposal additionally refuses any recorded witness that still exists. Generic
disposal never deletes staging files or destination content.

`dispose(target, discard=True)` explicitly fences an uncommitted started attempt
or acknowledges an existing receipt first. Planning failures/builds can also be
discarded. An unreadable Catalog, unknown ownership marker, unexpected file,
unresolved witness or ambiguous outcome retains evidence/path for inspection.
Explicit discard is not physical rollback; inspect the saved manifest/report
before discarding unresolved physical history. Failed/interrupted finalization
never triggers automatic cleanup. Cleanup failure after commit does not change
the receipt, republish, or prune anything. There is no automatic expiry, recovery
daemon or permanent per-file operation journal.
