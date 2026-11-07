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

Create a new schema-version-2 catalog at an unused path. The parent directory must exist; an existing database is rejected.

### `open`

```python
open(database_path: Path) -> Catalog
```

Open an existing schema-version-2 catalog after validation. Version 1 requires explicit migration; unsupported or invalid databases are rejected.

### `migrate`

```python
migrate(database_path: Path) -> bool
```

Explicitly migrate a version-1 catalog to version 2. Return `True` if migrated, `False` if already current; preserve content, observations, and catalog UUID.

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

Persistent database schema version, currently 2.

### `database_path`

```python
database_path: Path
```

Canonical absolute path of the SQLite database.

## Scanning and reconciliation

### `scan_directory`

```python
scan_directory(root: Path, *, excluded_directories: Collection[Path]=(), progress_callback: Callable[[ScanProgress], None] | None=None) -> ScanSummary
```

Reconcile the source and atomically apply its observations. Return the new successful scan summary; source files remain unchanged. Exclusions are directory paths, and the optional callback receives completed-file progress.

### `reconcile_directory`

```python
reconcile_directory(root: Path, *, excluded_directories: Collection[Path]=(), progress_callback: Callable[[ScanProgress], None] | None=None) -> RefreshChangeSet
```

Read the filesystem and compare it with current observations without writing catalog state. Return a deterministic change set with a catalog baseline. May reuse hashes when metadata is unchanged; the baseline does not lock filesystem bytes.

### `apply_refresh`

```python
apply_refresh(change_set: RefreshChangeSet) -> ScanSummary
```

Atomically publish a reconciled change set after checking its catalog baseline. Return the successful scan summary; a stale baseline raises `StaleRefreshError`. Does not reread or mutate source files.

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
along with retained scan/refresh/history records, provenance types, errors, and
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
