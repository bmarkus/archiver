# Plan 005 — Archiver Foundation

**Status:** In progress — 005.1–005.3 implemented; 005.4 and later children inactive.

Parent: None.  
Created: 2026-11-12.  
Implemented: 005.1 and 005.2 completed 2026-11-12; 005.3 completed 2026-11-13; remaining children not started.

## Goal

Introduce an Archiver that operates on existing Catalog instances for local
deduplication and one-way synchronization. A local deduplication operation needs
only one catalog and an explicit root; it does not require a destination.
Synchronization identifies source and target catalogs and roots explicitly.

The target Catalog is the persistent inventory. Archiver requires no permanent
inventory database or prescribed directory structure. A temporary, operation-scoped
SQLite sync workspace stores the copy manifest and execution progress; it is not
a merged Catalog or evidence that planned files already exist at the target.
Implement Catalog support first, independently of filesystem mutation.

Writing this plan does not authorize implementation or activate any child.
Complete and review each activated child before starting another.

## Accepted direction — 2026-11-12

- Initially publish complete successor inventory snapshots. For targeted updates,
  copy unaffected observations inside SQLite; do not rescan unaffected files.
- Provide one Catalog update boundary for additions, replacements, and removals.
  Archiver must not depend on the snapshot-copying implementation.
- Use opaque inventory revisions at that boundary so later incremental database
  writes need not change Archiver's operation model.
- For synchronization, build a temporary database-backed copy manifest, process
  physical copies and workspace progress in bounded batches, and publish verified
  successful entries to the target inventory once at finalization. Do not create
  a target snapshot per copy batch or materialize the full manifest in Python.
- Keep the latest **two successful snapshots per managed location by default**,
  counting full scans and targeted updates together. Permit an explicit positive
  retention count or keep-all policy. Prune only after a replacement succeeds,
  in the same database transaction as publication.
- Existing standalone Catalog scan/refresh defaults remain unchanged. Archiver
  explicitly passes its retention policy to updates and requested full scans.
- Historical path evidence is optional. Content identities and content-level tag
  assertions, retractions, and provenance survive snapshot pruning.
- Old database migration is not required. Unsupported versions must fail clearly;
  rebuilding by rescanning must be explicit and must never overwrite an old
  database automatically. Rescanning does not recover user tags or provenance.
- For schema version 3, opening or attempting migration of version 1 or 2 must
  fail without writes and explain that an explicit rebuild is required. Preserve
  the migration entry point; do not relabel an old schema as current.
- Inventories describe known file instances, not guaranteed complete directory
  coverage. Snapshot origin identifies the publication method, not completeness.
- Preparation reads one pinned baseline. Publication acquires the write lock
  before checking that baseline and rejects an existing transaction. Nonempty
  valid targeted requests publish even if content is unchanged; empty requests
  publish nothing and do not prune.

## Authoritative-document gate — satisfied by 005.1

The pre-005.1 domain model derived current state from the latest successful scan
and its retention addenda described explicit pruning only. 005.1 implemented
explicit domain-model and invariant amendments for successful inventory
publication from inherited observations plus targeted changes. Preserve the
historical Plan 001 and 002.3 outcomes. No content-identity, tag-history, or
filesystem-safety invariant may be weakened by later workspace/execution work.

The amendment distinguishes inherited snapshot membership from a fresh observation
event while preserving the identity and immutability of retained historical
snapshots. Inventories carry no directory-completeness guarantee. A sync manifest
is proposed work and recovery evidence, not an authoritative inventory.

## Subplans and sequence

| Child | Scope | Exit gate |
| --- | --- | --- |
| [005.1 — Targeted Catalog Updates and Inventory Retention](005.1-targeted-catalog-updates.md) | Snapshot-backed targeted updates, revision and publication boundaries, retention, explicit schema versioning. | Independently validated Catalog behavior and documented observation semantics. |
| [005.2 — Cross-catalog Content Comparison](005.2-cross-catalog-content-comparison.md) | Stream distinct current content absent from an explicit target location; evaluate SQLite ATTACH internally and provide bounded results for manifest construction. | Bounded Python memory, deterministic results, no source-record changes. |
| [005.3 — Operation Records and Execution Contract](005.3-operation-records-and-execution-contract.md) | Managed-root authority, plans/results, temporary sync-workspace lifecycle/statuses, target-side or local operation summaries, interruption and recovery contract. | Concrete manifest, finalization, and recovery contract before filesystem operations. |
| 005.4 — One-way Synchronization | Verified copying in bounded batches, relative destination paths, deterministic collision policy, database-backed successful-entry ingestion and one final target inventory publication. | Bounded-memory planning/copy/finalization, one successor snapshot, partial-failure reporting, and repeated-sync acceptance tests. |
| 005.5 — Local Deduplication | Deterministic survivor selection, explicit deletion, verification, and current-inventory removals. | Stale observations cannot alone authorize deletion; successful deletions are reflected in inventory. |
| 005.6 — Integration and Public Documentation | Cross-feature acceptance review, public examples and documentation. | Every umbrella criterion accounted for and required checks pass. |

005.1–005.3 are implemented. No child is currently active. Write
the remaining child plans before activating their work. Each child includes its
own tests and documentation; 005.6 does not defer validation from earlier children.

## Catalog prerequisites

Registration accepts one or multiple files in a specified root, reuses the
existing unchanged-metadata hash cache where appropriate, and supports forced
fresh hashing. It preserves existing current-file, duplicate, tag, and history
queries. Removal retires a path from current inventory without deleting content
identity or shared tags. Neither operation modifies physical files.

Targeted path validation rejects aliases of the same destination path, including
case aliases on case-insensitive filesystems, and checks symlink ancestors before
resolution can hide them. Existing hashing checks alone do not provide this guard.

Cross-catalog comparison uses identities represented by current file instances,
not all rows in the historical content table. Catalog UUIDs already identify
catalogs; record source UUID and useful location information in operation
summaries rather than adding another catalog-identity scheme. ATTACH is an
implementation option, not a public dependency. Do not physically merge catalogs
or copy their entire histories/tags into the workspace. Materialize only the
comparison results and operation data needed for planning and recovery.

005.1 remains the completed snapshot publication foundation. Its existing public
`prepare_file_update()` materializes a supplied batch, so passing an entire large
manifest to that API does not meet the sync memory requirement. Before sync
implementation, 005.4 must detail a narrow database-backed or streaming successful-
entry ingestion path behind the Catalog publication boundary, with validation,
fresh destination verification evidence, revision checking, and atomic retention.
Do not bypass that boundary by having Archiver write target inventory tables.
Internal chunking must still produce only one final successor snapshot, not one
snapshot per input chunk. This is future planned work, not a change to 005.1's
implementation or historical completion record.

## Archiver behavior

Planning is non-destructive, identifies exact roots and authority, and provides
counts and planned actions before execution. API names are provisional:

```python
archiver = Archiver()
plan = archiver.plan_dedup(catalog, root=local_root)
result = plan.execute()

plan = archiver.plan_sync(
    source_catalog,
    source_root=source_root,
    target=target_catalog,
    target_root=target_root,
)
result = plan.execute()
```

No mandatory full refresh occurs. Plans pin inventory revisions; execution
rejects stale database baselines and rechecks filesystem conditions for actions.
Sync planning may create/write the explicit temporary workspace, but must not
modify source/target inventory or physical source/target files. Execution-time
operation summaries are distinct from inventory snapshot publication.
Filesystem mutation and SQLite publication are not one atomic transaction.
Report physical success followed by registration failure as requiring
reconciliation, never as complete success. 005.3 must specify interruption,
recovery, batch boundaries, and operation-summary persistence before execution
is implemented. Detailed permanent per-file provenance is deferred; any minimal
recovery evidence needed for correctness must be explicitly justified.

### One-way synchronization

- Select one source instance deterministically per missing content identity.
- Preserve source-relative paths under the target root.
- Define a deterministic rename policy for collisions identified in planning.
  Skip and report unexpected execution-time conflicts; never overwrite.
- Copy to temporary storage on the destination filesystem, freshly SHA-256 verify
  the copied bytes against expected identity, and publish with no-overwrite
  semantics. A prior source verification cannot replace destination hashing.
- Register only verified, successfully placed files in one final target inventory
  publication after bounded copy processing. Leave source files and the source
  Catalog unchanged. Batch progress is committed to the workspace, not the target
  inventory.
- Record operation start/completion, status, source identity, copied/skipped/
  conflicting/failed counts in the target Catalog, separately from content tags.
- An inventory-only skip means cataloged presence, not guaranteed physical
  presence. 005.4 must document this limit and any optional targeted verification;
  stale target inventory must not be presented as a physical completeness check.

### Temporary sync workspace and finalization

1. Create an operation-scoped SQLite workspace with an operation identifier,
   source/target catalog UUIDs, explicit roots, and captured inventory revisions.
   Store a deterministic copy manifest: expected identity/size, selected source
   path, proposed target-relative path, status, and necessary failure/verification
   information. The manifest may be large; memory use must depend on batch size,
   not total operation size. Do not persist a permanent second inventory.
2. Compare current inventories and mark already-represented content only in the
   workspace/report. Do not tag or modify source records to indicate duplicates.
   A source observation is an available candidate, not an existing target copy.
3. Execute configurable bounded batches. For each file, check source/destination
   conditions, copy to a destination-filesystem temporary file, hash actual copied
   bytes, and place with no-overwrite semantics. Commit per-file outcomes to the
   workspace in bounded transactions. Close comparison readers before finalization
   and retention. Do not keep a target inventory write transaction open while copying.
4. Finalize once using only successful, verified destination entries. Revalidate
   the captured target inventory revision under the publication write lock. If it
   changed, fail finalization clearly and retain recovery evidence; never silently
   apply the old plan to the new baseline. Recheck source revision before execution;
   005.3 must specify responses to later source changes and destination changes
   between verification and final publication. No full refresh is required.
5. A normally completed attempt may contain failed/skipped files: publish its
   verified successes once and report partial completion accurately. If there are
   no successful entries, publish no successor snapshot. Default retention keeps
   two successful snapshots. Operation summary and the finalization receipt must
   be consistent with committed inventory; 005.3 must specify the target-side
   operation-ID/receipt mechanism for detecting an already committed attempt.
6. On interruption before final publication, the old target inventory stays
   current even though physical copies may already exist. Retain the workspace
   for explicit recovery, reverify destination files rather than trusting saved
   success flags, and validate/replan revisions before any resumed publication.
   Recovery must distinguish an uncommitted attempt from a committed publication
   whose acknowledgement/workspace update was interrupted. Never overwrite or
   delete existing destination files merely to retry an operation.
   Follow 005.3's explicit recovery transitions: a frozen request can only retry
   unchanged. Invalidated entries or baselines require explicit superseding plans
   with new operation IDs, durable bounded evidence handoff and fresh verification
   of reusable destinations at their existing paths, rather than copying them
   again. Old IDs are fenced against later publication in the target Catalog.
7. After confirmed finalization and a consistent terminal summary, remove the
   temporary workspace by default. Retain it on interrupted/failed finalization
   until explicit recovery or discard. Its location/lifetime must be documented
   and survive process exit when recovery is needed; it must not be mistaken for
   a Python temporary directory that is unconditionally deleted on exit. Exclude
   workspace databases/sidecars and in-flight copy artifacts from inventory inputs.
   A workspace is not long-term per-file provenance and is not subject to Catalog
   snapshot pruning. 005.3 will specify safe disposal and compatibility details.
   A finalized attempt with known outcomes may be cleaned even when its terminal
   status is failed; a failed finalization or uncertain physical outcome retains
   evidence. Unresolved artifact cleanup retains the workspace without changing
   an already committed receipt or inventory result.

One final publication avoids repeated full-inventory copying, but still requires
space/time for the complete successor snapshot and its SQLite transaction. A
persisted manifest does not make filesystem operations and database publication
atomic. These limits must appear in public examples and result reporting.

### Local deduplication

- Use existing duplicate queries and an explicit deterministic survivor policy.
- Execution grants deletion authority only within the planned managed root.
- Verify the retained instance and candidates against expected content before
  deletion. Never intentionally remove the final verified copy. Define external
  filesystem-concurrency limits rather than claiming an absolute race-free
  guarantee from catalog records.
- Remove successfully deleted paths from current inventory. Failed deletions
  remain recorded; stale/missing paths receive explicit outcomes.
- Do not use a content tag to describe whether an individual path exists.
- Persist the local operation summary in the same Catalog; no target is needed.

## Constraints and non-goals

Python-first, sqlite3, existing hashing and filesystem boundaries, deterministic
ordering, and explicit schema versions remain mandatory. No Rust, automatic
watchers/full scans, moves during sync, bidirectional sync, source deletion
during sync, directory reorganization, content-addressed storage, tag-management
expansion, merge policies, working-copy lifecycle, or advanced retention.

Complete snapshot copying is an accepted initial database cost, not a claim of
truly incremental writes. Optimize only after measurement and a later plan.
The temporary sync workspace is an explicit exception to the earlier no-separate-
database direction: no permanent Archiver inventory database is introduced. It
stores operation planning/progress only; the target Catalog stays authoritative.

## Umbrella acceptance criteria

1. 005.1 independently supports targeted additions/replacements/removals, preserves
   query behavior, and defaults managed publication to two successful snapshots.
2. Full refresh after targeted updates yields correct inventory; failed work
   preserves the last committed state and does not prune it.
3. Comparison covers overlapping, distinct, duplicate, empty, and historically
   known-but-no-longer-current content without unbounded Python catalog loading.
4. Sync handles nested paths, collisions, stale source/target observations,
   destination verification mismatch, interruption, and partial registration.
5. Repeated sync copies no additional content when inventories and files remain
   unchanged; no existing destination content is overwritten.
6. Dedup works with one local Catalog, retains a verified survivor, preserves
   shared tags, and reports partial deletion/publication failures accurately.
7. All mutation is explicitly scoped; planning/comparison preserve source and
   target files and inventory records, including on failure. Sync planning may
   write only its explicitly identified temporary workspace.
8. Persistence survives reopen; schema incompatibility is explicit; public
   examples cover local dedup, sync, retention, and recovery limitations.
9. Targeted preparation cannot mix baselines during concurrent publication or
   pruning. Publication validates revisions under its write lock; legacy open/
   migration attempts fail without altering the old database.

10. Large-manifest construction, copy processing, and final ingestion use bounded
    Python memory. Multiple copy batches produce one final successor snapshot,
    with only verified successes; empty success sets create no snapshot.
11. Target inventory remains unchanged before finalization. Partial copy failures
    produce accurate manifest/status counts and at most one final publication of
    successes. A stale target baseline rejects publication without pruning it.
12. Interruptions during copy, workspace progress commits, final publication,
    acknowledgement, and cleanup preserve recovery evidence as appropriate.
    Explicit recovery rechecks destinations, detects already committed attempts,
    and never duplicates registration or overwrites existing files to retry.
13. Successful completion cleans up the workspace; failed/interrupted finalization
    retains it for recovery/discard. Workspace/copy artifacts never become source
    or target inventory entries. Source catalogs/files remain unchanged, and no
    duplicate-status tags are written into the source.

## Review decisions — 2026-11-12

The consistency review is incorporated into this draft and 005.1: explicit
rebuild-only legacy handling, pinned preparation, known-instance semantics,
deterministic publication of nonempty updates, path-alias/ancestor validation,
and write-lock-before-baseline transaction ordering. Collision naming, survivor
selection, filesystem-concurrency limits, and recovery after physical success
and database failure remain decisions for 005.3–005.5, before those children are
implemented. Neither plan is activated by these revisions.

## Completion requirements

For each implementation child, run `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run mypy src tests`. Compare every child
acceptance criterion with implementation and summarize changed files, tests,
behavior, schema, decisions, and deviations. Do not start the next child
automatically. Planning-only changes do not claim these implementation checks.


## 005.1 completion checkpoint — 2026-11-12

At the user's explicit activation request, 005.1 implemented targeted Catalog
updates, schema 3 origin tracking, domain/invariant amendments, and atomic
publication retention defaulting to two successful targeted snapshots. All
required checks passed: 366 tests passed, 6 platform symlink skips; Ruff lint,
format, and mypy passed. See the child's acceptance review and limitations.
No Archiver execution or 005.2 implementation has begun. The remaining child
plans must still be written and separately activated.


## Sync workspace amendment — 2026-11-12 (planning only)

At the user's request, synchronization now uses an operation-scoped temporary
SQLite workspace, bounded copy/progress batches, and one final target inventory
publication of verified successes. This supersedes the earlier per-copy-batch
registration direction and prohibition on any separate Archiver database; a
permanent second inventory database is still excluded. The detailed lifecycle,
receipt/recovery contract, and large-result ingestion boundary belong to 005.3
and 005.4 before their activation. Batch-size defaults, workspace placement, and
concrete receipt/schema/API details must be settled in those child plans.

Only this umbrella plan was amended. No code, schema, 005.1 implementation record,
or authoritative domain/invariant changes are required by this planning update.
No child is activated; 005.2 remains inactive and implementation has not started.

## 005.2 planning checkpoint — 2026-11-12

[The detailed 005.2 draft](005.2-cross-catalog-content-comparison.md) specifies
read-only scoped comparison sessions, complete SQL summaries, deterministic
classified identity batches, and the handoff to future workspace construction.
It defines snapshot/lifetime/read-only limits and acceptance tests without
implementing a workspace or copy engine. 005.2 is not activated; no implementation
began as part of this planning checkpoint.

## 005.2 completion checkpoint — 2026-11-12

At the user's explicit activation request, 005.2 implemented read-only comparison
sessions over captured current inventories, complete SQL summaries, deterministic
representatives, and bounded classified identity batches. Schema 3 is unchanged.
All required checks passed: 416 tests passed, 6 platform symlink skips; Ruff lint,
format, and mypy passed. The public example also ran successfully. See the
child's acceptance review for coverage and read-only/snapshot limits.
No workspace, copy engine, or 005.3 work was introduced; later children remain
inactive and require separate plans and activation.

## 005.3 planning checkpoint — 2026-11-13

[The detailed 005.3 draft](005.3-operation-records-and-execution-contract.md)
assigns the temporary SQLite sync database's model, manifest/progress storage,
lifecycle and recovery contract to 005.3. Production manifest construction,
collision naming, copying, destination verification and bounded final ingestion
remain in 005.4. Local physical deletion remains in 005.5.

The draft specifies schema-4 minimal operation summaries/receipts and a narrow,
explicit additive schema-3 migration to preserve existing inventory and tag data.
This is a planned extension of the earlier rebuild-only compatibility boundary,
not a rewrite of 005.1's schema-3 behavior: versions 1 and 2 still require explicit
rebuild, and opening unsupported versions still performs no writes. No permanent
per-file manifest or second inventory is introduced. Receipt persistence and
inventory publication share one transaction; actual filesystem execution remains
outside that transaction. No child is activated by this planning checkpoint.

## 005.3 consistency review amendment — 2026-11-13 (planning only)

The revised child specifies lifecycle transitions and receipt-first recovery,
immutable frozen requests, explicit supersession and safe evidence handoff, and
receipt/outcome-based disposal rules. Workspace format 1 owns the placement
evidence contract, `retained_link_v1`: retain an operation-owned destination-
filesystem staging file as a witness, place using no-replace hard-link creation,
and recover uncertain placement only after same-file checks and fresh hashing.
This is a temporary placement mechanism; it adds no permanent storage layout,
content identity scheme or duplicate-resolution policy. 005.4 implements and
tests it and must reject unsupported filesystems before copying; a different
protocol requires an explicit plan/compatibility amendment. 005.3 validates the
records and protocol decisions using synthetic evidence, without copying files.

The temporary workspace remains operation-scoped; the target Catalog remains
the permanent inventory. There is still at most one successor snapshot per
finalized attempt, and no snapshot for an empty successful-entry set. No source
deletion, destination overwrite, automatic rebase or automatic full scan is
introduced. No implementation or activation is authorized by this amendment.

## 005.3 completion checkpoint — 2026-11-13

At the user's explicit activation request, 005.3 implemented bounded temporary
SQLite workspace storage, explicit authority and lifecycle records, retained-link
intent/evidence validation, immutable requests, receipt-first recovery decisions,
publication fencing, evidence handoff and owned-workspace disposal. Schema 4 adds
minimal permanent operation summaries/receipts; an explicit additive schema-3
migration preserves inventory, revisions, tags and provenance. Inventory,
retention and the terminal receipt commit in one Catalog transaction. No permanent
per-file manifest or second inventory was introduced.

Validation: 454 tests passed, 7 platform symlink skips; Ruff lint/format, mypy,
the temporary-data example and documentation/diff checks passed. The child's
outcome maps every acceptance criterion to evidence and records the scope limits.
The retained-link filesystem adapter, production/replacement manifest planning,
copy execution and bounded successful-entry ingestion remain 005.4 work. No
005.4 or physical deduplication work was started. No child is currently active.
