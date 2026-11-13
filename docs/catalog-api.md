# Catalog API reference

This reference documents the implemented public `Catalog` surface. Import
`Catalog`, domain records, errors, and query/result classes from `archiver`.
Import expression factories (`content`, `duplicate`, `mtime`, `path`, `size`,
`tags`, `tag_name`, `tag_usage`) from `archiver.query`. Query objects are created
through the four catalog entry points, not by supplying private constructor fields.
See [query grammar](query-grammar.md) for transformations, terminals, and bounds.

```python
from pathlib import Path
from archiver import Catalog

with Catalog.create(Path("catalog.sqlite")) as catalog:
    catalog.scan_directory(Path("source"))
    rows = catalog.files(Path("source")).limit(10).fetch()
```

Signatures below omit implicit `self`/`cls`. `Path` and `PurePosixPath` come
from `pathlib`; `Callable`, `Collection`, and `Iterator` come from
`collections.abc`. All other annotation names are available from `archiver`,
except `sqlite3.Connection` in the low-level initializer. `create`, `open`, and
`migrate` are class methods; the other methods operate on a catalog instance.
Properties are read-only.

## Cross-catalog content comparison

### `compare_content`

```python
compare_content(target: Catalog, *, source_root: Path, target_root: Path) -> ContentComparison
```

Create a read-only comparison session over two explicit roots. Roots must be
accessible directories, and Catalog handles must be open with no pending
transactions. Enter the session before reading results:

```python
with source.compare_content(target, source_root=source_root, target_root=target_root) as comparison:
    summary = comparison.summary()
    for batch in comparison.batches(batch_size=1000):
        for row in batch:
            print(row.content_id, row.source_relative_path)
```

The session compares distinct `(algorithm, digest)` identities in captured current
inventories, including targeted publications. Historical content, retired paths,
and other roots do not contribute. An unregistered/unscanned root has an empty
known set; `ComparisonScope.has_inventory` distinguishes it from an observed
empty snapshot. Same-database and same-root comparisons are supported.

`ContentComparison.summary()` returns an immutable `ContentComparisonSummary`
with `source_content_count`, `target_content_count`, `shared_content_count`,
`missing_source_content_count`, `target_only_content_count`, and
`bytes_to_copy`. Counts are complete regardless of batch size.
`bytes_to_copy` is the file-content bytes needed to copy missing source content
to the target, counting each missing identity once regardless of duplicate paths.
It estimates copy bytes, not actual free-space demand.

`batches(*, batch_size: int = 1000, include_present: bool = False)` returns a
closeable generator of immutable tuples of `ComparedContent`, ordered by full
algorithm/digest. Each row has `content_id`, `size_bytes`, `source_relative_path`,
`source_mtime_ns`, `source_instance_count`, and `in_target`. The representative is
the smallest complete POSIX relative path under SQLite BINARY ordering. Default
batches contain missing source content; `include_present=True` includes every
source identity with its presence classification. Batch sizes must be integers
in `1..2**63-1` (not bool); `include_present` must be bool. A size is an upper bound;
internal fetches cap batches at 10,000 rows without allocating the requested
maximum in advance. SQL grouping/sorting can use space proportional to inventory
size even though Python memory is bounded by a batch.

`source_scope` and `target_scope` expose immutable `ComparisonScope` records:
`catalog_uuid`, canonical `root`, `location`, opaque `revision`, and
`has_inventory`. Location IDs are catalog-local metadata; use revisions for
baseline checks. Existing `ContentId` SHA-256 validation is unchanged. Malformed
scoped records and contradictory sizes fail clearly, including invalid records
that would otherwise be omitted from returned batches.

Only one batch iterator may be active. Exhaust it or call its `close()` before
another traversal. After an early `break`, explicitly close that iterator or
leave the session context. Closing an iterator releases its cursor; subsequent
traversals still use the same snapshots. Session `close()` is idempotent and
context exit closes abandoned iterators, the read transaction, and its dedicated
connection. Closed sessions cannot be reentered. SQL-backed reads before entry
or after closure fail. Already returned scope, summary, and batch values remain
ordinary immutable data. Caller Catalog handles remain open and untouched.

Each catalog is captured independently in a read transaction: the pair is stable,
but does not represent a globally atomic instant across databases. WAL permits
concurrent writers while preserving the captured view; rollback journaling may
block writers until the session closes, subject to SQLite's bounded timeout.
Close the session before later copy, publication, or retention work. A new
session captures new committed inventories.

Inputs use readonly SQLite attachments and query-only protection, without schema
changes. Opening checks UUIDs and available filesystem identity; these checks
cannot detect every concurrent file replacement or a same-UUID copy substituted
before the checks. SQLite-managed sidecar/locking activity may occur. No source
files are scanned, hashed, opened, or changed. Cataloged presence is not physical
verification, and the selected source path is only a candidate. Comparison does
not create a workspace, manifest, tags, or snapshots. `ContentComparisonError`
reports catalog, SQL, consistency, and session errors; wrapped failures preserve
their cause. Invalid arguments use `TypeError`/`ValueError`. Legacy schemas fail
with rebuild guidance; rebuilding does not restore tags/provenance.

See the runnable [comparison example](../examples/compare_content.py), which uses
only temporary files and catalogs, and the step-by-step
[Plan 005.2 notebook](../notebooks/public/plan-005.2-cross-catalog-content-comparison.ipynb).

## Lifecycle and properties

### `__init__`

```python
__init__(database_path: Path, connection: sqlite3.Connection) -> None
```

Low-level initializer for an existing SQLite connection; enables foreign keys and row materialization. Normal callers should use `create()` or `open()` so schema validation is performed.

### `create`

```python
create(database_path: Path) -> Catalog
```

Create a new schema-version-3 catalog at an unused path. The parent directory must exist; an existing database is rejected.

### `open`

```python
open(database_path: Path) -> Catalog
```

Open an existing schema-version-3 catalog after validation. Versions 1 and 2 require an explicit rebuild in a separate database; unsupported or invalid databases are rejected.

### `migrate`

```python
migrate(database_path: Path) -> bool
```

Validate a version-3 catalog and return `False` (already current). Versions 1 and 2 are rejected without writes, with explicit rebuild guidance. Rescanning recovers file inventory, not user tags or provenance. Never overwrite an old catalog to rebuild it.

### `close`

```python
close() -> None
```

Close the SQLite connection. Close active iterators before the catalog.

### `__enter__`

```python
__enter__() -> Catalog
```

Return this catalog for use in a `with` statement.

### `__exit__`

```python
__exit__(exc_type: object, exc_value: object, traceback: object) -> None
```

Close this catalog on leaving a `with` statement; exceptions are not suppressed.

### `catalog_uuid`

```python
catalog_uuid: str
```

Persistent catalog UUID, read from metadata.

### `schema_version`

```python
schema_version: int
```

Persistent database schema version, currently 3.

### `database_path`

```python
database_path: Path
```

Canonical absolute path of the SQLite database.

## Scanning and reconciliation

### `scan_directory`

```python
scan_directory(root: Path, *, verify_hashes: bool=False, keep_last: int | None=None, excluded_directories: Collection[Path]=(), progress_callback: Callable[[ScanProgress], None] | None=None) -> ScanSummary
```

Reconcile the source and atomically apply its observations. Return the new successful scan summary; source files remain unchanged. Exclusions are directory paths, and the optional callback receives completed-file progress (observed sizes, not bytes physically read). Default refresh reuses matching metadata; `verify_hashes=True` hashes every included regular file. Both modes preserve exclusions, symlink rules, and failure atomicity.

### `reconcile_directory`

```python
reconcile_directory(root: Path, *, verify_hashes: bool=False, excluded_directories: Collection[Path]=(), progress_callback: Callable[[ScanProgress], None] | None=None) -> RefreshChangeSet
```

Read the filesystem and compare it with current observations without writing catalog state. Return a deterministic change set with a catalog baseline. By default, matching relative path, size, and nanosecond mtime reuse the cached SHA-256 identity. This is a cache heuristic: a same-size rewrite with restored mtime can be missed. Pass `verify_hashes=True` to read and SHA-256 hash every included regular file, including duplicate copies. Fresh identities determine unchanged/modified classification; unchanged verified files have `hash_reused=False`. The baseline does not lock filesystem bytes.

### `apply_refresh`

```python
apply_refresh(change_set: RefreshChangeSet, *, keep_last: int | None=None) -> ScanSummary
```

Atomically publish a reconciled change set after checking its catalog baseline. Return the successful scan summary; a stale baseline raises `StaleRefreshError`. Does not reread or mutate source files.

Full verification records bytes observed during reconciliation. Application does not reread them, and later filesystem changes remain possible. No verification timestamp is persisted.

Reconciliation changes use deterministic depth-first POSIX path-component order:
`bundle/file.txt` precedes `bundle.zip`. The baseline and filesystem traversal
use this same order to classify each path once. Public query path sorting
continues to order complete relative-path strings.

Future archive ingestion must independently verify the destination bytes with SHA-256 against the expected identity before accepting a managed copy, even if the source catalog already has a hash or was fully verified. Archive ingestion is not implemented here; see [Plan 002.2](plans/002.2-refresh-hash-verification.md).

## Query entry points

### `files`

```python
files(root: Path) -> CurrentFileQuery
```

Build a lazy query for one root’s latest successful file observations. Default: 20 rows, relative path ascending. `fetch()` returns `tuple[FileObservation, ...]`.

### `duplicates`

```python
duplicates(root: Path) -> DuplicateGroupQuery
```

Build a lazy query for current content groups with more than one file instance at one root. Default: 20 groups, 20 members each, redundant bytes descending with full identity ascending ties. `fetch()` returns `tuple[DuplicateGroupView, ...]`.

The CLI `--details` defaults to a metrics-only table; positive `--member-limit N`
adds filenames. This does not change the Python query’s positive member limit
or its default of 20. See the [CLI reference](catalog-cli.md).

Use `offset(count).limit(count)` on this returned query for group pages; offsets
are zero-based, default to zero, and apply to the current successful scan at
execution. See [duplicate pagination](query-grammar.md#duplicate-pagination) for
refresh behavior and `stream_all()` suffix semantics. No `Catalog` method
signature changes are required.

### `contents`

```python
contents() -> ContentQuery
```

Build a lazy catalog-wide content query, including untagged identities and identities without current paths. Default: 20 rows, full algorithm/digest ascending. `fetch()` returns `tuple[ContentRecord, ...]`.

### `tags`

```python
tags() -> TagVocabularyQuery
```

Build a lazy catalog-wide query over canonical names with active assertions. Default: 20 names, name ascending, all provenance. `fetch()` returns `tuple[TagUsage, ...]`.

## Exact and historical reads

### `current_scan`

```python
current_scan(root: Path) -> ScanRun | None
```

Return the latest successful scan for the root, or `None` for a valid unknown/unscanned root.

### `current_summary`

```python
current_summary(root: Path) -> ScanSummary | None
```

Return the current successful scan’s summary, or `None` for a valid unknown/unscanned root.

### `content_for_path`

```python
content_for_path(root: Path, relative_path: PurePosixPath) -> ContentId
```

Resolve an exact current POSIX relative path to content identity. A missing current path raises `TaggingError` rather than returning an empty search result.

### `tags_for_content`

```python
tags_for_content(content_id: ContentId) -> tuple[ContentTagAssertion, ...]
```

Return all active provenance-aware assertions for known content, ordered by tag, provenance kind, source name/version/detail. Known untagged content returns an empty tuple; unknown content raises `TaggingError`.

### `tags_for_path`

```python
tags_for_path(root: Path, relative_path: PurePosixPath) -> tuple[ContentTagAssertion, ...]
```

Resolve a current path and return its content’s active assertions in the same order as `tags_for_content()`.

### `scan_history`

```python
scan_history() -> Iterator[ScanRun]
```

Lazily yield all persisted scan runs, including running/failed rows, ordered by location root then scan ID. Keep the catalog open during consumption.

### `scan_summaries`

```python
scan_summaries(root: Path) -> Iterator[ScanHistorySummary]
```

Stream all persisted scans for one root, newest scan ID first. Each immutable
`ScanHistorySummary` contains `scan: ScanRun`, `files_observed`,
`total_bytes_observed`, and `is_current`. Scan metadata supplies ID, location,
status, start time, and optional completion time. Counts include duplicate
physical copies and empty files; empty scans appear with zero totals. Failed
and running scan totals are partial persisted observations, not current inventory.
Reads begin on first advancement; keep the catalog open and close a partially
consumed iterator with `contextlib.closing`. Unknown/unscanned roots yield no rows.

### `prune_history`

```python
prune_history(root: Path, *, keep_last: int=3, dry_run: bool=True, include_failed: bool=False) -> PruneHistorySummary
```

Explicitly preview or delete old history for one root. Retain the newest N
successful (`completed`) scans by descending ID, with a minimum of 1; timestamps
do not determine retention. Default `dry_run=True` writes nothing. Application
recomputes its own candidates and deletes observations before scan records in
one transaction. A preview report is not a deletion token.

`include_failed=True` also removes failed scans before the oldest retained
successful scan. Newer failures and all running scans stay. Without any
successful scan there is no cutoff and no failed cleanup. Keeping fewer than
N successes still allows opt-in failures before the oldest success to be removed.
Current scans referenced by any location are protected. Content, tags, all
assertions/retractions/provenance, and other roots are preserved, including
content with no remaining file observations. Retained IDs are never renumbered.

`PruneHistorySummary` has `root`, `keep_last`, `dry_run`, `include_failed`,
`successful_scans_retained`, `oldest_retained_scan_id` (or `None`),
`scans_to_remove`, `observations_to_remove`, `successful_scans_to_remove`, and
`failed_scans_to_remove`. Counts describe candidates in preview mode and actual
removed rows after successful application. The two status counts sum to total
scans removed. No unbounded ID list is materialized.

`keep_last` must be an exact integer in `1..2**63-1` (excluding bool); wrong types
raise `TypeError`, out-of-range values raise `ValueError`. Both flags require
actual bools. Existing root rules apply: an accessible directory, no symlink
root. An unknown/unscanned root returns zero counts without creating a location.
An existing transaction or protected current-scan candidate raises
`HistoryPruningError`. SQLite failures are wrapped in that error with their
cause. Application/read-transaction errors and interruptions roll back; a caller's
existing transaction is never committed or rolled back by this operation.

Finish or close all history/summary/file/group/member iterators before applying
pruning. Do not interleave maintenance and iteration on this or another
connection. Materialized values survive. Duplicate handles retain their members
across refresh and while their scan is retained; loading members after that scan
is pruned raises `PrunedScanError` on first advancement. Handles remain bound to
the originating open catalog; closed-catalog errors retain their normal behavior.

No pruning happens automatically during refresh/open/close. `VACUUM` is separate
optional maintenance, not part of this API. Deleted pages are reusable by SQLite;
the file need not shrink, and preserving all content identities means this is
not a permanent bound on database size. Offline-root maintenance and running-scan
recovery are outside this operation.

```python
for item in catalog.scan_summaries(root):
    print(item.scan.id, item.scan.status, item.files_observed, item.total_bytes_observed)

preview = catalog.prune_history(root, keep_last=3, include_failed=True)
print(preview.scans_to_remove, preview.observations_to_remove)
# Explicitly apply after reviewing; this call recomputes the candidate set.
result = catalog.prune_history(root, keep_last=3, include_failed=True, dry_run=False)
```

### `observation_history`

```python
observation_history() -> Iterator[HistoricalObservation]
```

Lazily yield all persisted observations with scan identity/status, ordered by location root, scan ID, and relative path. Keep the catalog open during consumption.

## Tag mutations

### `add_content_tag`

```python
add_content_tag(content_id: ContentId, tag: str, provenance: TagProvenance) -> bool
```

Add an active assertion for known content and exact producer provenance. Return whether an assertion was added; repeating the same active assertion returns `False`.

### `add_tag_for_path`

```python
add_tag_for_path(root: Path, relative_path: PurePosixPath, tag: str, provenance: TagProvenance) -> bool
```

Resolve a current path and add an assertion to its content, shared by every identical copy. Return whether an assertion was added.

### `retract_content_tag`

```python
retract_content_tag(content_id: ContentId, tag: str, provenance: TagProvenance | None=None) -> int
```

Retract matching active assertions without erasing history. Omitted provenance retracts all user assertions for this content/tag; explicit provenance matches that exact producer, including system producers. Return the number retracted.

### `retract_user_tag_for_path`

```python
retract_user_tag_for_path(root: Path, relative_path: PurePosixPath, tag: str) -> int
```

Resolve a current path and retract every active user assertion for its content/tag. Return the number retracted; system assertions remain active.

## Validation, execution, and authority

Root-taking operations validate that the root exists, is a directory, and is
not a symbolic link (`ScanFailure` on failure). Query roots and detail-view
roots are validated/canonicalized eagerly; persisted state is resolved at
execution. A valid unknown root yields empty file/duplicate rows and zero
reductions; content-detail roots affect only path previews, not content selection.
Exact path helpers require a non-empty `PurePosixPath` without absolute paths,
parent traversal, or backslashes (`ValueError`). Unknown content and missing
current paths raise `TaggingError`. Invalid canonical tag names raise `ValueError`.
Database creation/open/migration failures raise `InvalidCatalogError`.
Reconciliation/application failures raise `RefreshFailure` (a `ScanFailure`);
failed or interrupted work never replaces the last successful current state.

Query construction and immutable transformations are lazy with eager argument
validation. Limits must be exact positive integers, excluding `bool`:
wrong types raise `TypeError`, unsupported values raise `ValueError`.
`all_tags` must be a `bool`; numeric tag limits remain validated even when
`all_tags=True`. Regex factories validate eagerly and retain `re.error`.
Cross-domain expressions are rejected. There is no raw SQL construction API.

Fetches are bounded tuples. File/content detail views have their own query
and row types; creating a detail view never changes the base query’s result
type. Detail views expose `fetch()`; content details also expose
`current_path_count()`. Set filters/order/row bounds on the base before creating
a view. Displayed tags are distinct alphabetical active names across all
provenance; `all_tags=True` removes only their preview bound, keeping parent
rows bounded. Complete counts, totals, and duplicate summaries ignore display
bounds. Each multi-statement root terminal pins one successful observation
scan; tag reads do not promise an atomic snapshot across statements.

Every terminal needs the catalog open. Streaming and history iterators execute
on first advancement and own independent cursors. Exhaustion, explicit close,
or errors release their cursors; use `contextlib.closing` for early exit.
A plain `break` does not guarantee cleanup. Duplicate handles retain their
resolved scan after refresh or outer-stream closure and require the original
catalog open for member reads. Separate terminals can see different current
scans. See the [stream lifecycle tutorial](../notebooks/public/query-stream-lifecycle.ipynb).

Scans, reconciliation, and queries do not modify source files. Applying refreshes
and tag mutations changes catalog metadata only; catalog records confer no
archive storage authority. No schema changes were made in Plan 004.7.

## Imports and adapters

All query classes (`CurrentFileQuery`, `CurrentFileTagQuery`, `ContentQuery`,
`ContentDetailQuery`, `TagVocabularyQuery`, `DuplicateGroupQuery`, and
`DuplicateGroupHandle`) are available from `archiver`. Result classes
`FileObservation`, `CurrentFileTagView`, `ContentRecord`, `ContentTagView`,
`TagUsage`, `DuplicateGroupView`, and `DuplicateSummary` are also available there,
along with `ScanHistorySummary`, `PruneHistorySummary`, `HistoryPruningError`,
`PrunedScanError`, retained scan/refresh/history records, provenance types, errors, and
`validate_tag_name`. Expression factories belong in `archiver.query`.

The separate adapters `current_files_frame`, `observation_history_frame`, and
`scan_history_frame` live in `archiver.pandas`. They consume typed iterables
and materialize pandas DataFrames; passing an unbounded stream intentionally
materializes all its rows. Pandas is not part of query execution.

## Migration from the earlier API

These pre-1.0 removals have no compatibility shims. All unlisted lifecycle,
exact/history, and mutation methods retain their signatures.

| Retired method | Supported replacement |
| --- | --- |
| `current_files(root)` | `files(root).stream_all()`; explicitly collect if needed |
| `search_current_files(...)` | `files(root)` filters/order/bounds plus separate reductions |
| `find_by_content(root, id)` | `files(root).where(content.is_(id))` |
| `search_current_files_with_tags(...)` | `files(root)` predicates and `with_tags()` |
| `search_tagged_content(...)` | `contents()` predicates; `tags_for_content(id)` for assertions |
| `search_content_by_tags(...)` | `contents()` predicates and `with_details(root=...)` |
| `search_available_tags(...)` | `tags()` vocabulary query |
| `duplicate_groups`, `search_duplicate_groups`, `duplicate_summary` | `duplicates(root)` fetch/stream/summary terminals |

Retired result envelopes: `CurrentFileSearch`, `TagAwareCurrentFileSearch`,
`TaggedContentSearch`, `TaggedContent`, `MultiTagContentSearch`,
`AvailableTagSearch`, and `DuplicateGroupSearch`. Retired aliases:
`CurrentFileSort`, `AvailableTagSort`, `TagMatchMode`. CLI parsing uses local
types. The tuple-oriented `duplicate_groups_frame` adapter is also retired.
Historical notebooks explicitly identify their required frozen revision;
current tutorials and examples use the supported surface.


## Targeted inventory updates

For an executable walkthrough using temporary files, see the
[Plan 005.1 public tutorial](../notebooks/public/plan-005.1-targeted-catalog-updates.ipynb).

### `prepare_file_update`

```python
prepare_file_update(root: Path, *, paths: Path | Collection[Path]=(), remove_paths: Collection[PurePosixPath]=(), verify_hashes: bool=False) -> InventoryChanges
```

Prepare selected regular-file observations and catalog-only removals without
writing SQLite or traversing the root. `paths` accepts a single Path or a
collection, absolute or relative to `root`. Removals are root-relative POSIX
paths and must exist in the baseline inventory, even if physically deleted.
Reject duplicate/conflicting paths, case aliases on Windows, out-of-root paths,
symlink/junction ancestors, and the catalog's database/sidecars. Distinct
hard-linked names are allowed. Matching metadata reuses cached identity;
`verify_hashes=True` hashes every supplied file. Cached reads share one pinned
baseline, and hashing occurs after releasing its read transaction.

### `apply_inventory_changes`

```python
apply_inventory_changes(changes: InventoryChanges, *, keep_last: int | None=2) -> InventoryUpdateResult
```

Publish a prepared change set against its opaque catalog/root-scoped revision.
Callers obtain changes from preparation, not by constructing observations with
asserted digests. An empty request checks the baseline but creates no snapshot
and performs no pruning. Every nonempty request creates a successor, including
unchanged upserts (counted as replacements). Returns `InventoryUpdateResult`:
`revision`, `origin`, inserted/replaced/removed path counts, full `inventory`
totals, and optional `retention` counts. Content/tag history survives removals.

Both full and targeted publication acquire a write lock before checking the
baseline and reject an existing transaction without changing pending work.
Stale or foreign targeted changes raise `StaleRefreshError`; invalid input raises
`TypeError`/`ValueError`; database publication failures raise `RefreshFailure`.
Publication failures roll back the current pointer, new rows, and retention.
Neither preparation nor publication modifies physical files.

Targeted publication defaults to `keep_last=2`; `None` retains all, and positive
integers retain that many successful snapshots per root, across both origins.
Full `scan_directory()` and `apply_refresh()` accept the same option but default
to keep-all. Retention is part of the publication transaction. Running/failed
snapshots, other roots, content, and tags are preserved. No VACUUM runs; deleted
pages are reusable rather than necessarily shrinking the database file.

Close active streams before requesting pruning. Duplicate handles pinned to
retained snapshots still work; handles for pruned snapshots raise
`PrunedScanError`. `ScanRun.origin` is `full_scan` or `targeted_update` and is
available in current, history, and observation-history records and CLI summaries.
Inventory totals describe known instances, not fresh full-directory observations
or a coverage guarantee. Inherited snapshot membership is not a new observation.
Physical changes after observation remain possible; path checks do not lock the
filesystem. A later full refresh observes the root normally.

```python
from pathlib import Path, PurePosixPath
from archiver import Catalog

root = Path("source").resolve()
with Catalog.create(Path("new-catalog.sqlite")) as catalog:
    catalog.scan_directory(root)  # keep-all full-scan default
    changes = catalog.prepare_file_update(root, paths=Path("incoming/file.txt"))
    result = catalog.apply_inventory_changes(changes)  # two successful snapshots
    # Retire a catalog instance only; this does not delete the physical file.
    changes = catalog.prepare_file_update(root, remove_paths=[PurePosixPath("incoming/file.txt")])
    catalog.apply_inventory_changes(changes)
    catalog.scan_directory(root, keep_last=2)  # discovers a still-present file again
```

Initial registration without a prior scan is supported and inventories only
selected files. Full successor snapshots copy untouched rows inside SQLite:
O(total inventory size) database work per batch, without rehashing those files.
This persistence detail is behind the Catalog publication boundary; revision
internals are opaque. Truly incremental database writes and Archiver operations
are not implemented by Plan 005.1.
