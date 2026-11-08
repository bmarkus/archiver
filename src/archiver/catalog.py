"""SQLite persistence and read-only local directory scanning."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Iterator
from contextlib import closing, suppress
from pathlib import Path, PurePosixPath
from time import monotonic, time_ns
from uuid import uuid4

from ._query import (
    ContentQueryDescription,
    CurrentObservationQuery,
    DuplicateGroupQueryDescription,
    TagVocabularyQueryDescription,
)
from ._sqlite_query import (
    execute_content_count,
    execute_content_current_path_count,
    execute_content_details,
    execute_content_for_current_path,
    execute_content_record,
    execute_contents,
    execute_current_observation_count,
    execute_current_observation_distinct_content_count,
    execute_current_observation_tag_views,
    execute_current_observation_total_size,
    execute_current_scan,
    execute_duplicate_groups,
    execute_duplicate_summary,
    execute_first_current_observation,
    execute_scan_summary,
    execute_tag_vocabulary,
    execute_tag_vocabulary_count,
    execute_tags_for_content,
    iter_current_observations,
    iter_duplicate_group_members,
    iter_duplicate_group_rows,
    iter_observation_history,
    iter_scan_history,
)
from .errors import InvalidCatalogError, RefreshFailure, ScanFailure, StaleRefreshError, TaggingError
from .filesystem import regular_files
from .hashing import hash_file_stably
from .models import (
    ContentId,
    ContentRecord,
    ContentTagAssertion,
    ContentTagView,
    CurrentFileTagView,
    DuplicateGroupView,
    DuplicateSummary,
    FileObservation,
    HistoricalObservation,
    Location,
    RefreshChange,
    RefreshChangeKind,
    RefreshChangeSet,
    ScanProgress,
    ScanRun,
    ScanSummary,
    TagProvenance,
    TagUsage,
    validate_tag_name,
)
from .query import (
    ContentDetailQuery,
    ContentQuery,
    CurrentFileQuery,
    CurrentFileTagQuery,
    DuplicateGroupHandle,
    DuplicateGroupQuery,
    TagVocabularyQuery,
    duplicate,
)

SCHEMA_VERSION = 2
_TAG_NAME_REGEX_FUNCTION = "archiver_tag_name_regex"

_SCHEMA = """
CREATE TABLE catalog_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    catalog_uuid TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    current_scan_id INTEGER REFERENCES scan_runs(id)
);
CREATE TABLE content (
    id INTEGER PRIMARY KEY,
    algorithm TEXT NOT NULL,
    digest TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    UNIQUE (algorithm, digest)
);
CREATE TABLE scan_runs (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    started_at_ns INTEGER NOT NULL,
    completed_at_ns INTEGER,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed'))
);
CREATE TABLE file_observations (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES scan_runs(id),
    relative_path TEXT NOT NULL,
    content_id INTEGER NOT NULL REFERENCES content(id),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    mtime_ns INTEGER NOT NULL,
    UNIQUE (scan_id, relative_path)
);
CREATE INDEX file_observations_scan_path ON file_observations (scan_id, relative_path);
CREATE INDEX file_observations_content ON file_observations (content_id);
CREATE TABLE tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE content_tag_assertions (
    id INTEGER PRIMARY KEY,
    content_id INTEGER NOT NULL REFERENCES content(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    provenance_kind TEXT NOT NULL CHECK (provenance_kind IN ('user', 'system')),
    source_name TEXT NOT NULL CHECK (length(source_name) > 0),
    source_version TEXT NOT NULL CHECK (length(source_version) > 0),
    source_detail TEXT NOT NULL DEFAULT '',
    asserted_at_ns INTEGER NOT NULL,
    retracted_at_ns INTEGER,
    CHECK (retracted_at_ns IS NULL OR retracted_at_ns >= asserted_at_ns)
);
CREATE UNIQUE INDEX content_tag_assertions_active_source
ON content_tag_assertions (
    content_id, tag_id, provenance_kind, source_name, source_version, source_detail
)
WHERE retracted_at_ns IS NULL;
CREATE INDEX content_tag_assertions_content_active
ON content_tag_assertions (content_id, tag_id)
WHERE retracted_at_ns IS NULL;
CREATE INDEX content_tag_assertions_tag_active
ON content_tag_assertions (tag_id, content_id)
WHERE retracted_at_ns IS NULL;
"""

_TAG_SCHEMA_STATEMENTS = (
    "CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
    """
    CREATE TABLE content_tag_assertions (
        id INTEGER PRIMARY KEY,
        content_id INTEGER NOT NULL REFERENCES content(id),
        tag_id INTEGER NOT NULL REFERENCES tags(id),
        provenance_kind TEXT NOT NULL CHECK (provenance_kind IN ('user', 'system')),
        source_name TEXT NOT NULL CHECK (length(source_name) > 0),
        source_version TEXT NOT NULL CHECK (length(source_version) > 0),
        source_detail TEXT NOT NULL DEFAULT '',
        asserted_at_ns INTEGER NOT NULL,
        retracted_at_ns INTEGER,
        CHECK (retracted_at_ns IS NULL OR retracted_at_ns >= asserted_at_ns)
    )
    """,
    """
    CREATE UNIQUE INDEX content_tag_assertions_active_source
    ON content_tag_assertions (
        content_id, tag_id, provenance_kind, source_name, source_version, source_detail
    )
    WHERE retracted_at_ns IS NULL
    """,
    """
    CREATE INDEX content_tag_assertions_content_active
    ON content_tag_assertions (content_id, tag_id)
    WHERE retracted_at_ns IS NULL
    """,
    """
    CREATE INDEX content_tag_assertions_tag_active
    ON content_tag_assertions (tag_id, content_id)
    WHERE retracted_at_ns IS NULL
    """,
)

_BASE_SCHEMA_TABLES = frozenset({"catalog_metadata", "locations", "content", "scan_runs", "file_observations"})
_TAG_SCHEMA_TABLES = frozenset({"tags", "content_tag_assertions"})


def _compare_relative_path_components(left: str, right: str) -> int:
    """Match depth-first traversal and PurePosixPath merge comparisons."""
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    return (left_parts > right_parts) - (left_parts < right_parts)


def _validate_schema_tables(connection: sqlite3.Connection, version: int) -> None:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    actual = {str(row[0]) for row in rows}
    required = _BASE_SCHEMA_TABLES | (_TAG_SCHEMA_TABLES if version == SCHEMA_VERSION else frozenset())
    missing = sorted(required - actual)
    if missing:
        raise InvalidCatalogError(f"catalog schema is missing required tables: {', '.join(missing)}")


class Catalog:
    """A persistent, observational catalog of local regular files."""

    def __init__(self, database_path: Path, connection: sqlite3.Connection) -> None:
        self._database_path = database_path.resolve(strict=False)
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.create_collation("archiver_path_components", _compare_relative_path_components)
        self._connection.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def create(cls, database_path: Path) -> Catalog:
        """Create a new schema-version-2 catalog at an unused path."""
        path = database_path.resolve(strict=False)
        if path.exists():
            raise InvalidCatalogError(f"catalog database already exists: {path}")
        try:
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO catalog_metadata (singleton, catalog_uuid, schema_version) VALUES (1, ?, ?)",
                    (str(uuid4()), SCHEMA_VERSION),
                )
        except sqlite3.Error as error:
            with suppress(UnboundLocalError):
                connection.close()
            raise InvalidCatalogError(f"could not create catalog: {path}") from error
        return cls(path, connection)

    @classmethod
    def migrate(cls, database_path: Path) -> bool:
        """Explicitly migrate a schema-version-1 catalog to version 2.

        Return ``True`` when a migration was applied and ``False`` when the
        catalog was already current.
        """
        path = database_path.resolve(strict=False)
        if not path.is_file():
            raise InvalidCatalogError(f"catalog database does not exist: {path}")
        try:
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            row = connection.execute(
                "SELECT catalog_uuid, schema_version FROM catalog_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise InvalidCatalogError("catalog metadata is missing")
            version = row[1]
            if version == SCHEMA_VERSION:
                _validate_schema_tables(connection, SCHEMA_VERSION)
                connection.close()
                return False
            if version != 1:
                raise InvalidCatalogError(
                    f"unsupported catalog schema version: {version} (expected 1 or {SCHEMA_VERSION})"
                )
            _validate_schema_tables(connection, 1)
            with connection:
                for statement in _TAG_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE catalog_metadata SET schema_version = ? WHERE singleton = 1",
                    (SCHEMA_VERSION,),
                )
                _validate_schema_tables(connection, SCHEMA_VERSION)
        except sqlite3.Error as error:
            with suppress(UnboundLocalError):
                connection.close()
            raise InvalidCatalogError(f"could not migrate catalog: {path}") from error
        except InvalidCatalogError:
            connection.close()
            raise
        connection.close()
        return True

    @classmethod
    def open(cls, database_path: Path) -> Catalog:
        """Open an existing catalog after validating its schema version."""
        path = database_path.resolve(strict=False)
        if not path.is_file():
            raise InvalidCatalogError(f"catalog database does not exist: {path}")
        try:
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            row = connection.execute(
                "SELECT catalog_uuid, schema_version FROM catalog_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise InvalidCatalogError("catalog metadata is missing")
            version = row[1]
            if version == 1:
                raise InvalidCatalogError(
                    "catalog schema version 1 requires migration; run 'archiver catalog migrate ROOT'"
                )
            if version != SCHEMA_VERSION:
                raise InvalidCatalogError(f"unsupported catalog schema version: {version} (expected {SCHEMA_VERSION})")
            _validate_schema_tables(connection, SCHEMA_VERSION)
        except sqlite3.Error as error:
            with suppress(UnboundLocalError):
                connection.close()
            raise InvalidCatalogError(f"could not open catalog: {path}") from error
        except InvalidCatalogError:
            connection.close()
            raise
        return cls(path, connection)

    @property
    def catalog_uuid(self) -> str:
        """Return the catalog's persistent UUID."""
        row = self._connection.execute("SELECT catalog_uuid FROM catalog_metadata WHERE singleton = 1").fetchone()
        assert row is not None
        return str(row["catalog_uuid"])

    @property
    def schema_version(self) -> int:
        """Return the catalog schema version."""
        row = self._connection.execute("SELECT schema_version FROM catalog_metadata WHERE singleton = 1").fetchone()
        assert row is not None
        return int(row["schema_version"])

    @property
    def database_path(self) -> Path:
        """Return the canonical path of this catalog's SQLite database."""
        return self._database_path

    def close(self) -> None:
        """Close the SQLite connection."""
        self._connection.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def scan_directory(
        self,
        root: Path,
        *,
        verify_hashes: bool = False,
        excluded_directories: Collection[Path] = (),
        progress_callback: Callable[[ScanProgress], None] | None = None,
    ) -> ScanSummary:
        """Reconcile and apply observations, optionally hashing every included file."""
        change_set = self.reconcile_directory(
            root,
            verify_hashes=verify_hashes,
            excluded_directories=excluded_directories,
            progress_callback=progress_callback,
        )
        return self.apply_refresh(change_set)

    def reconcile_directory(
        self,
        root: Path,
        *,
        verify_hashes: bool = False,
        excluded_directories: Collection[Path] = (),
        progress_callback: Callable[[ScanProgress], None] | None = None,
    ) -> RefreshChangeSet:
        """Observe a directory without writing to the catalog.

        Its baseline identifies the catalog-current scan used for comparison. It
        does not lock the filesystem; observations can become stale after this
        method returns and before a caller applies the returned change set.
        Matching path, size, and mtime normally reuse a cached identity. Set
        verify_hashes to read and SHA-256 hash every included regular file.
        """
        canonical_root = self._canonical_root(root)
        existing_location = self._location_for_root(canonical_root)
        location = existing_location if existing_location is not None else Location(id=None, root=canonical_root)
        baseline_scan = self.current_scan(canonical_root)
        baseline_scan_id = None if baseline_scan is None else baseline_scan.id
        previous_observations = (
            self._current_observation_iterator(location) if existing_location is not None else iter(())
        )
        previous = next(previous_observations, None)
        changes: list[RefreshChange] = []
        started_at = monotonic()
        files_observed = 0
        total_bytes_observed = 0

        try:
            # Both streams must use component order, not complete path-string
            # order: a directory's descendants precede a sibling like dir.zip.
            for path in regular_files(canonical_root, excluded_directories):
                if self._is_catalog_file(path):
                    continue
                relative_path = PurePosixPath(path.relative_to(canonical_root).as_posix())
                while previous is not None and previous.relative_path < relative_path:
                    changes.append(RefreshChange("missing", previous.relative_path, previous, None, hash_reused=False))
                    previous = next(previous_observations, None)

                if previous is not None and previous.relative_path == relative_path:
                    current, hash_reused = self._reconcile_existing_path(
                        path, location, previous, verify_hashes=verify_hashes
                    )
                    kind: RefreshChangeKind = "unchanged" if current.content_id == previous.content_id else "modified"
                    changes.append(RefreshChange(kind, relative_path, previous, current, hash_reused))
                    previous = next(previous_observations, None)
                else:
                    current = self._hashed_observation(path, location, relative_path)
                    changes.append(RefreshChange("new", relative_path, None, current, hash_reused=False))

                files_observed += 1
                total_bytes_observed += current.size_bytes
                if progress_callback is not None:
                    progress_callback(
                        ScanProgress(
                            files_observed=files_observed,
                            total_bytes_observed=total_bytes_observed,
                            elapsed_seconds=monotonic() - started_at,
                            current_relative_path=relative_path,
                        )
                    )
            while previous is not None:
                changes.append(RefreshChange("missing", previous.relative_path, previous, None, hash_reused=False))
                previous = next(previous_observations, None)
        except Exception as error:
            if isinstance(error, RefreshFailure):
                raise
            raise RefreshFailure(f"reconciliation failed for {canonical_root}") from error

        return RefreshChangeSet(location=location, baseline_scan_id=baseline_scan_id, changes=tuple(changes))

    def apply_refresh(self, change_set: RefreshChangeSet) -> ScanSummary:
        """Atomically apply observations after checking their catalog baseline."""
        try:
            with self._connection:
                location = self._location_for_refresh_apply(change_set)
                assert location.id is not None
                current_scan_id = self._current_scan_id(location.id)
                if current_scan_id != change_set.baseline_scan_id:
                    raise StaleRefreshError("refresh change set no longer matches the catalog current state")
                scan_id = self._insert_running_scan(location.id)
                for change in change_set.changes:
                    if change.current is None:
                        continue
                    current = change.current
                    if current.location.root != location.root:
                        raise RefreshFailure("refresh change set contains an observation for a different location")
                    content_row_id = self._get_or_create_content(current.content_id, current.size_bytes)
                    self._connection.execute(
                        """
                        INSERT INTO file_observations (scan_id, relative_path, content_id, size_bytes, mtime_ns)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            scan_id,
                            current.relative_path.as_posix(),
                            content_row_id,
                            current.size_bytes,
                            current.mtime_ns,
                        ),
                    )
                self._complete_scan(location.id, scan_id)
        except StaleRefreshError:
            raise
        except Exception as error:
            if isinstance(error, RefreshFailure):
                raise
            raise RefreshFailure(f"could not apply refresh for {change_set.location.root}") from error
        return self._scan_summary(scan_id)

    def add_content_tag(self, content_id: ContentId, tag: str, provenance: TagProvenance) -> bool:
        """Add one active assertion, returning whether catalog state changed."""
        canonical_tag = validate_tag_name(tag)
        content_row_id, _ = self._content_record(content_id)
        with self._connection:
            self._connection.execute(
                "INSERT INTO tags (name) VALUES (?) ON CONFLICT (name) DO NOTHING", (canonical_tag,)
            )
            tag_row = self._connection.execute("SELECT id FROM tags WHERE name = ?", (canonical_tag,)).fetchone()
            assert tag_row is not None
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO content_tag_assertions (
                    content_id, tag_id, provenance_kind, source_name, source_version,
                    source_detail, asserted_at_ns, retracted_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    content_row_id,
                    int(tag_row["id"]),
                    provenance.kind,
                    provenance.source_name,
                    provenance.source_version,
                    provenance.source_detail,
                    time_ns(),
                ),
            )
        return cursor.rowcount == 1

    def add_tag_for_path(
        self,
        root: Path,
        relative_path: PurePosixPath,
        tag: str,
        provenance: TagProvenance,
    ) -> bool:
        """Resolve a current path and add a tag assertion to its content."""
        return self.add_content_tag(self._content_for_current_path(root, relative_path), tag, provenance)

    def retract_content_tag(
        self,
        content_id: ContentId,
        tag: str,
        provenance: TagProvenance | None = None,
    ) -> int:
        """Retract an exact provenance, or every active user assertion when omitted."""
        canonical_tag = validate_tag_name(tag)
        content_row_id, _ = self._content_record(content_id)
        parameters: list[object] = [time_ns(), content_row_id, canonical_tag]
        provenance_clause = "AND assertions.provenance_kind = 'user'"
        if provenance is not None:
            provenance_clause = """
                AND assertions.provenance_kind = ?
                AND assertions.source_name = ?
                AND assertions.source_version = ?
                AND assertions.source_detail = ?
            """
            parameters.extend(
                (
                    provenance.kind,
                    provenance.source_name,
                    provenance.source_version,
                    provenance.source_detail,
                )
            )
        with self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE content_tag_assertions AS assertions
                SET retracted_at_ns = ?
                WHERE assertions.content_id = ?
                  AND assertions.tag_id = (SELECT id FROM tags WHERE name = ?)
                  AND assertions.retracted_at_ns IS NULL
                  {provenance_clause}
                """,
                parameters,
            )
        return cursor.rowcount

    def retract_user_tag_for_path(self, root: Path, relative_path: PurePosixPath, tag: str) -> int:
        """Retract all active user assertions for a current path's content."""
        return self.retract_content_tag(self._content_for_current_path(root, relative_path), tag)

    def tags_for_content(self, content_id: ContentId) -> tuple[ContentTagAssertion, ...]:
        """Return active assertions for known content in deterministic order."""
        content_row_id, _ = self._content_record(content_id)
        return execute_tags_for_content(self._connection, content_row_id, content_id)

    def content_for_path(self, root: Path, relative_path: PurePosixPath) -> ContentId:
        """Resolve a current relative path to its content identity."""
        return self._content_for_current_path(root, relative_path)

    def tags_for_path(self, root: Path, relative_path: PurePosixPath) -> tuple[ContentTagAssertion, ...]:
        """Return active content tags resolved through a current relative path."""
        return self.tags_for_content(self._content_for_current_path(root, relative_path))

    def contents(self) -> ContentQuery:
        """Create a lazy, bounded query over every persisted content identity."""
        return ContentQuery(_catalog=self)

    def tags(self) -> TagVocabularyQuery:
        """Create a lazy, bounded active-tag vocabulary query."""
        return TagVocabularyQuery(_catalog=self)

    @staticmethod
    def _tag_vocabulary_description(query: TagVocabularyQuery, *, limit: int | None) -> TagVocabularyQueryDescription:
        return TagVocabularyQueryDescription(
            predicates=query.predicates,
            ordering=query.ordering,
            limit=limit,
            provenance=query.provenance_kind,
        )

    def _fetch_tag_vocabulary_query(self, query: TagVocabularyQuery) -> tuple[TagUsage, ...]:
        return execute_tag_vocabulary(
            self._connection,
            self._tag_vocabulary_description(query, limit=query.row_limit),
        )

    def _count_tag_vocabulary_query(self, query: TagVocabularyQuery) -> int:
        return execute_tag_vocabulary_count(
            self._connection,
            self._tag_vocabulary_description(query, limit=None),
        )

    def _content_query_description(self, query: ContentQuery, *, limit: int | None) -> ContentQueryDescription:
        return ContentQueryDescription(predicates=query.predicates, limit=limit)

    def _fetch_content_query(self, query: ContentQuery) -> tuple[ContentRecord, ...]:
        return execute_contents(
            self._connection,
            self._content_query_description(query, limit=query.row_limit),
        )

    def _count_content_query(self, query: ContentQuery) -> int:
        return execute_content_count(self._connection, self._content_query_description(query, limit=None))

    def _content_detail_scope(self, query: ContentDetailQuery) -> tuple[Location | None, int | None]:
        location = self._location_for_canonical_root(query.root)
        if location is None:
            return None, None
        assert location.id is not None
        return location, self._current_scan_id(location.id)

    def _fetch_content_detail_query(self, query: ContentDetailQuery) -> tuple[ContentTagView, ...]:
        location, scan_id = self._content_detail_scope(query)
        return execute_content_details(
            self._connection,
            self._content_query_description(query.base, limit=query.base.row_limit),
            location=location,
            scan_id=scan_id,
            path_limit=query.path_limit,
            tag_limit=None if query.all_tags else query.tag_limit,
        )

    def _count_content_detail_paths(self, query: ContentDetailQuery) -> int:
        _, scan_id = self._content_detail_scope(query)
        return execute_content_current_path_count(
            self._connection,
            self._content_query_description(query.base, limit=None),
            scan_id=scan_id,
        )

    def files(self, root: Path) -> CurrentFileQuery:
        """Create a lazy, bounded query over one root's current observations.

        The root is validated and canonicalized immediately. Filtering,
        ordering, and the latest successful scan are resolved only when the
        returned query's fetch() method runs. See docs/query-grammar.md for the
        supported public grammar.
        """
        return CurrentFileQuery(root=self._canonical_root(root), _catalog=self)

    def _fetch_current_file_query(self, query: CurrentFileQuery) -> tuple[FileObservation, ...]:
        return tuple(self._stream_current_file_query(query, limit=query.row_limit))

    def _fetch_current_file_tag_query(self, query: CurrentFileTagQuery) -> tuple[CurrentFileTagView, ...]:
        internal_query = self._current_observation_query(query.base, limit=query.base.row_limit)
        if internal_query is None:
            return ()
        return execute_current_observation_tag_views(
            self._connection,
            internal_query,
            tag_limit=None if query.all_tags else query.tag_limit,
        )

    def _first_current_file_query(self, query: CurrentFileQuery) -> FileObservation | None:
        internal_query = self._current_observation_query(query, limit=None)
        return None if internal_query is None else execute_first_current_observation(self._connection, internal_query)

    def _count_current_file_query(self, query: CurrentFileQuery) -> int:
        internal_query = self._current_observation_query(query, limit=None)
        return 0 if internal_query is None else execute_current_observation_count(self._connection, internal_query)

    def _total_size_current_file_query(self, query: CurrentFileQuery) -> int:
        internal_query = self._current_observation_query(query, limit=None)
        return 0 if internal_query is None else execute_current_observation_total_size(self._connection, internal_query)

    def _distinct_content_count_current_file_query(self, query: CurrentFileQuery) -> int:
        internal_query = self._current_observation_query(query, limit=None)
        return (
            0
            if internal_query is None
            else execute_current_observation_distinct_content_count(self._connection, internal_query)
        )

    def _stream_current_file_query(self, query: CurrentFileQuery, *, limit: int | None) -> Iterator[FileObservation]:
        internal_query = self._current_observation_query(query, limit=limit)
        if internal_query is not None:
            yield from iter_current_observations(self._connection, internal_query)

    def _current_observation_query(
        self, query: CurrentFileQuery, *, limit: int | None
    ) -> CurrentObservationQuery | None:
        location = self._location_for_canonical_root(query.root)
        if location is None:
            return None
        assert location.id is not None
        scan_id = self._current_scan_id(location.id)
        if scan_id is None:
            return None
        return CurrentObservationQuery(
            location=location,
            scan_id=scan_id,
            predicates=query.predicates,
            ordering=query.ordering,
            limit=limit,
        )

    def duplicates(self, root: Path) -> DuplicateGroupQuery:
        """Create a lazy, bounded query over one root's current duplicate groups."""
        return DuplicateGroupQuery(root=self._canonical_root(root), _catalog=self)

    def _fetch_duplicate_group_query(self, query: DuplicateGroupQuery) -> tuple[DuplicateGroupView, ...]:
        internal_query = self._duplicate_group_query(query, group_limit=query.group_limit)
        return () if internal_query is None else execute_duplicate_groups(self._connection, internal_query)

    def _summarize_duplicate_group_query(self, query: DuplicateGroupQuery) -> DuplicateSummary:
        internal_query = self._duplicate_group_query(query, group_limit=query.group_limit)
        return (
            DuplicateSummary(0, 0, 0)
            if internal_query is None
            else execute_duplicate_summary(self._connection, internal_query)
        )

    def _stream_duplicate_group_query(
        self, query: DuplicateGroupQuery, *, limit: int | None
    ) -> Iterator[DuplicateGroupHandle]:
        internal_query = self._duplicate_group_query(query, group_limit=limit)
        if internal_query is None:
            return
        with closing(iter_duplicate_group_rows(self._connection, internal_query)) as rows:
            for row in rows:
                yield DuplicateGroupHandle(
                    content_id=ContentId(str(row["algorithm"]), str(row["digest"])),
                    size_bytes=int(row["size_bytes"]),
                    file_instance_count=int(row["file_instance_count"]),
                    potential_redundant_bytes=int(row["potential_redundant_bytes"]),
                    _catalog=self,
                    _location=internal_query.location,
                    _scan_id=internal_query.scan_id,
                    _member_row_limit=query.member_row_limit,
                )

    def _stream_duplicate_group_members(
        self, handle: DuplicateGroupHandle, *, limit: int | None
    ) -> Iterator[FileObservation]:
        internal_query = DuplicateGroupQueryDescription(
            location=handle._location,
            scan_id=handle._scan_id,
            ordering=duplicate.redundant_bytes.desc(),
            group_limit=None,
            member_limit=limit,
        )
        yield from iter_duplicate_group_members(self._connection, internal_query, handle.content_id)

    def _duplicate_group_query(
        self, query: DuplicateGroupQuery, *, group_limit: int | None
    ) -> DuplicateGroupQueryDescription | None:
        location = self._location_for_canonical_root(query.root)
        if location is None:
            return None
        assert location.id is not None
        scan_id = self._current_scan_id(location.id)
        if scan_id is None:
            return None
        return DuplicateGroupQueryDescription(
            location=location,
            scan_id=scan_id,
            ordering=query.ordering,
            group_limit=group_limit,
            member_limit=query.member_row_limit,
            group_offset=query.group_offset,
        )

    def current_scan(self, root: Path) -> ScanRun | None:
        """Return the most recent successful scan for one location, if one exists."""
        location = self._location_for_root(root)
        if location is None:
            return None
        return execute_current_scan(self._connection, location)

    def current_summary(self, root: Path) -> ScanSummary | None:
        """Return the summary for the current successful scan, if one exists."""
        current_scan = self.current_scan(root)
        return None if current_scan is None else self._scan_summary(current_scan.id)

    def scan_history(self) -> Iterator[ScanRun]:
        """Yield every scan run in deterministic location-root and scan-ID order.

        The catalog must remain open while consuming the iterator.
        """
        yield from iter_scan_history(self._connection)

    def observation_history(self) -> Iterator[HistoricalObservation]:
        """Yield every persisted file observation in deterministic history order.

        The catalog must remain open while consuming the iterator.
        """
        yield from iter_observation_history(self._connection)

    def _content_record(self, content_id: ContentId) -> tuple[int, int]:
        record = execute_content_record(self._connection, content_id)
        if record is None:
            raise TaggingError(f"content is not known to this catalog: {content_id.algorithm}:{content_id.digest}")
        return record

    def _content_for_current_path(self, root: Path, relative_path: PurePosixPath) -> ContentId:
        canonical_path = self._validated_relative_path(relative_path)
        location = self._location_for_root(root)
        if location is None:
            raise TaggingError(f"catalog has no current path: {canonical_path.as_posix()}")
        content_id = execute_content_for_current_path(self._connection, location, canonical_path)
        if content_id is None:
            raise TaggingError(f"catalog has no current path: {canonical_path.as_posix()}")
        return content_id

    @staticmethod
    def _validated_relative_path(relative_path: PurePosixPath) -> PurePosixPath:
        text = relative_path.as_posix()
        if relative_path.is_absolute() or text in ("", ".") or ".." in relative_path.parts or "\\" in text:
            raise ValueError("tag path must be a non-empty POSIX relative path without parent traversal")
        return relative_path

    def _canonical_root(self, root: Path) -> Path:
        if root.is_symlink():
            raise ScanFailure("a scan root must not be a symbolic link")
        if not root.is_dir():
            raise ScanFailure(f"scan root is not a directory: {root}")
        return root.resolve(strict=True)

    def _location_for_root(self, root: Path) -> Location | None:
        canonical_root = self._canonical_root(root)
        return self._location_for_canonical_root(canonical_root)

    def _location_for_canonical_root(self, canonical_root: Path) -> Location | None:
        row = self._connection.execute(
            "SELECT id, root_path FROM locations WHERE root_path = ?", (str(canonical_root),)
        ).fetchone()
        return None if row is None else Location(id=int(row["id"]), root=Path(str(row["root_path"])))

    def _location_for_refresh_apply(self, change_set: RefreshChangeSet) -> Location:
        """Return the persisted location for an application transaction."""
        root = change_set.location.root
        row = self._connection.execute(
            "SELECT id, root_path FROM locations WHERE root_path = ?", (str(root),)
        ).fetchone()
        if row is None:
            if change_set.location.id is not None:
                raise StaleRefreshError("refresh location no longer exists in the catalog")
            cursor = self._connection.execute("INSERT INTO locations (root_path) VALUES (?)", (str(root),))
            assert cursor.lastrowid is not None
            return Location(id=int(cursor.lastrowid), root=root)
        location = Location(id=int(row["id"]), root=Path(str(row["root_path"])))
        if change_set.location.id is not None and change_set.location.id != location.id:
            raise StaleRefreshError("refresh location no longer matches the catalog")
        return location

    def _current_scan_id(self, location_id: int) -> int | None:
        row = self._connection.execute("SELECT current_scan_id FROM locations WHERE id = ?", (location_id,)).fetchone()
        if row is None:
            raise StaleRefreshError("refresh location no longer exists in the catalog")
        value = row["current_scan_id"]
        return None if value is None else int(value)

    def _insert_running_scan(self, location_id: int) -> int:
        cursor = self._connection.execute(
            "INSERT INTO scan_runs (location_id, started_at_ns, status) VALUES (?, ?, 'running')",
            (location_id, time_ns()),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def _complete_scan(self, location_id: int, scan_id: int) -> None:
        self._connection.execute(
            "UPDATE scan_runs SET status = 'completed', completed_at_ns = ? WHERE id = ?",
            (time_ns(), scan_id),
        )
        self._connection.execute(
            "UPDATE locations SET current_scan_id = ? WHERE id = ?",
            (scan_id, location_id),
        )

    def _reconcile_existing_path(
        self, path: Path, location: Location, previous: FileObservation, *, verify_hashes: bool
    ) -> tuple[FileObservation, bool]:
        if path.is_symlink() or not path.is_file():
            raise RefreshFailure(f"file changed type during reconciliation: {path}")
        metadata = path.stat()
        if not verify_hashes and metadata.st_size == previous.size_bytes and metadata.st_mtime_ns == previous.mtime_ns:
            return (
                FileObservation(
                    location=location,
                    relative_path=previous.relative_path,
                    content_id=previous.content_id,
                    size_bytes=int(metadata.st_size),
                    mtime_ns=int(metadata.st_mtime_ns),
                ),
                True,
            )
        return self._hashed_observation(path, location, previous.relative_path), False

    @staticmethod
    def _hashed_observation(path: Path, location: Location, relative_path: PurePosixPath) -> FileObservation:
        content_id, size_bytes, mtime_ns = hash_file_stably(path)
        return FileObservation(
            location=location,
            relative_path=relative_path,
            content_id=content_id,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
        )

    def _get_or_create_content(self, content_id: ContentId, size_bytes: int) -> int:
        row = self._connection.execute(
            "SELECT id, size_bytes FROM content WHERE algorithm = ? AND digest = ?",
            (content_id.algorithm, content_id.digest),
        ).fetchone()
        if row is not None:
            if int(row["size_bytes"]) != size_bytes:
                raise RefreshFailure("one content identity was observed with contradictory sizes")
            return int(row["id"])
        cursor = self._connection.execute(
            "INSERT INTO content (algorithm, digest, size_bytes) VALUES (?, ?, ?)",
            (content_id.algorithm, content_id.digest, size_bytes),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def _is_catalog_file(self, path: Path) -> bool:
        database_files = {
            self._database_path,
            Path(f"{self._database_path}-journal"),
            Path(f"{self._database_path}-shm"),
            Path(f"{self._database_path}-wal"),
        }
        return path.resolve(strict=False) in database_files

    def _scan_summary(self, scan_id: int) -> ScanSummary:
        return execute_scan_summary(self._connection, scan_id)

    def _current_observation_iterator(self, location: Location) -> Iterator[FileObservation]:
        """Stream current observations in the traversal's POSIX component order."""
        assert location.id is not None
        cursor = self._connection.execute(
            """
            SELECT observations.relative_path, observations.size_bytes, observations.mtime_ns,
                   content.algorithm, content.digest
            FROM file_observations AS observations
            JOIN content ON content.id = observations.content_id
            JOIN locations ON locations.current_scan_id = observations.scan_id
            WHERE locations.id = ?
            ORDER BY observations.relative_path COLLATE archiver_path_components
            """,
            (location.id,),
        )
        for row in cursor:
            yield FileObservation(
                location=location,
                relative_path=PurePosixPath(str(row["relative_path"])),
                content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
                size_bytes=int(row["size_bytes"]),
                mtime_ns=int(row["mtime_ns"]),
            )
