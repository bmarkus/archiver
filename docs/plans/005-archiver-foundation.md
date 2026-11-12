# Plan 005 — Archiver Foundation

**Status:** In progress — only 005.1 activated and implemented; later children inactive.

Parent: None.  
Created: 2026-11-12.  
Implemented: 005.1 completed 2026-11-12; remaining children not started.

## Goal

Introduce an Archiver that operates on existing Catalog instances for local
deduplication and one-way synchronization. A local deduplication operation needs
only one catalog and an explicit root; it does not require a destination.
Synchronization identifies source and target catalogs and roots explicitly.

The target Catalog is the persistent inventory. Archiver requires neither its
own database nor a prescribed directory structure. Implement Catalog support
first, independently of filesystem mutation.

Writing this plan does not authorize implementation or activate any child.
Complete and review each activated child before starting another.

## Accepted direction — 2026-11-12

- Initially publish complete successor inventory snapshots. For targeted updates,
  copy unaffected observations inside SQLite; do not rescan unaffected files.
- Provide one Catalog update boundary for additions, replacements, and removals.
  Archiver must not depend on the snapshot-copying implementation.
- Use opaque inventory revisions at that boundary so later incremental database
  writes need not change Archiver's operation model.
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

## Authoritative-document gate

The current domain model derives current state from the latest successful scan
and the retention addenda describe explicit pruning only. Targeted successor
snapshots and operation-selected automatic retention extend that model.

Before implementation, 005.1 must amend the domain model and invariants explicitly
to recognize successful inventory publication from inherited observations plus
targeted changes. Until then these are proposed semantics, not permission to
reinterpret the authoritative documents. Preserve the historical Plan 001 and
002.3 outcomes. No content-identity, tag-history, or filesystem-safety invariant
may be weakened.

The amendment must also distinguish inherited snapshot membership from a fresh
observation event while preserving the identity and immutability of retained
historical snapshots. Inventories carry no directory-completeness guarantee.

## Subplans and sequence

| Child | Scope | Exit gate |
| --- | --- | --- |
| [005.1 — Targeted Catalog Updates and Inventory Retention](005.1-targeted-catalog-updates.md) | Snapshot-backed targeted updates, revision and publication boundaries, retention, explicit schema versioning. | Independently validated Catalog behavior and documented observation semantics. |
| 005.2 — Cross-catalog Content Comparison | Stream distinct current content absent from an explicit target location; evaluate SQLite ATTACH internally. | Bounded Python memory, deterministic results, no source-record changes. |
| 005.3 — Operation Records and Execution Contract | Managed-root authority, plans/results, target-side or local operation summaries, partial failure and recovery contract. | Concrete execution and persistence contract before filesystem operations. |
| 005.4 — One-way Synchronization | Verified copying, relative destination paths, deterministic collision policy, incremental target registration. | Safe publication, partial-failure reporting, and repeated-sync acceptance tests. |
| 005.5 — Local Deduplication | Deterministic survivor selection, explicit deletion, verification, and current-inventory removals. | Stale observations cannot alone authorize deletion; successful deletions are reflected in inventory. |
| 005.6 — Integration and Public Documentation | Cross-feature acceptance review, public examples and documentation. | Every umbrella criterion accounted for and required checks pass. |

Only 005.1 is detailed and implemented now. Write the remaining child plans before activating
their work. Each child includes its own tests and documentation; 005.6 does not
defer validation from earlier children.

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
implementation option, not a public dependency. Do not physically merge catalogs.

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
- Register only verified, successfully published files, in batches. Leave the
  source files and source Catalog unchanged.
- Record operation start/completion, status, source identity, copied/skipped/
  conflicting/failed counts in the target Catalog, separately from content tags.
- An inventory-only skip means cataloged presence, not guaranteed physical
  presence. 005.4 must document this limit and any optional targeted verification;
  stale target inventory must not be presented as a physical completeness check.

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
7. All mutation is explicitly scoped; read-only planning and comparison preserve
   files and source records, including on failure.
8. Persistence survives reopen; schema incompatibility is explicit; public
   examples cover local dedup, sync, retention, and recovery limitations.
9. Targeted preparation cannot mix baselines during concurrent publication or
   pruning. Publication validates revisions under its write lock; legacy open/
   migration attempts fail without altering the old database.

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
