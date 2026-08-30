"""SQLite persistence and read-only local directory scanning."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Collection, Iterator
from contextlib import suppress
from pathlib import Path, PurePosixPath
from time import monotonic, time_ns
from typing import cast
from uuid import uuid4

from .errors import InvalidCatalogError, RefreshFailure, ScanFailure, StaleRefreshError, TaggingError
from .filesystem import regular_files
from .hashing import hash_file_stably
from .models import (
    AvailableTagSearch,
    AvailableTagSort,
    ContentId,
    ContentTagAssertion,
    ContentTagView,
    CurrentFileSearch,
    CurrentFileSort,
    CurrentFileTagView,
    DuplicateGroupSearch,
    DuplicateGroupView,
    DuplicateSummary,
    FileObservation,
    HistoricalObservation,
    Location,
    MultiTagContentSearch,
    RefreshChange,
    RefreshChangeKind,
    RefreshChangeSet,
    ScanProgress,
    ScanRun,
    ScanSummary,
    TagAwareCurrentFileSearch,
    TaggedContent,
    TaggedContentSearch,
    TagMatchMode,
    TagProvenance,
    TagProvenanceKind,
    TagUsage,
    validate_tag_name,
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
        excluded_directories: Collection[Path] = (),
        progress_callback: Callable[[ScanProgress], None] | None = None,
    ) -> ScanSummary:
        """Reconcile a directory then atomically apply its successful observations."""
        change_set = self.reconcile_directory(
            root,
            excluded_directories=excluded_directories,
            progress_callback=progress_callback,
        )
        return self.apply_refresh(change_set)

    def reconcile_directory(
        self,
        root: Path,
        *,
        excluded_directories: Collection[Path] = (),
        progress_callback: Callable[[ScanProgress], None] | None = None,
    ) -> RefreshChangeSet:
        """Observe a directory without writing to the catalog.

        Its baseline identifies the catalog-current scan used for comparison. It
        does not lock the filesystem; observations can become stale after this
        method returns and before a caller applies the returned change set.
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
            for path in regular_files(canonical_root, excluded_directories):
                if self._is_catalog_file(path):
                    continue
                relative_path = PurePosixPath(path.relative_to(canonical_root).as_posix())
                while previous is not None and previous.relative_path < relative_path:
                    changes.append(RefreshChange("missing", previous.relative_path, previous, None, hash_reused=False))
                    previous = next(previous_observations, None)

                if previous is not None and previous.relative_path == relative_path:
                    current, hash_reused = self._reconcile_existing_path(path, location, previous)
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
        rows = self._connection.execute(
            """
            SELECT
                tags.name AS tag_name,
                assertions.provenance_kind AS provenance_kind,
                assertions.source_name AS source_name,
                assertions.source_version AS source_version,
                assertions.source_detail AS source_detail,
                assertions.asserted_at_ns AS asserted_at_ns
            FROM content_tag_assertions AS assertions
            JOIN tags ON tags.id = assertions.tag_id
            WHERE assertions.content_id = ? AND assertions.retracted_at_ns IS NULL
            ORDER BY
                tags.name,
                assertions.provenance_kind,
                assertions.source_name,
                assertions.source_version,
                assertions.source_detail,
                assertions.asserted_at_ns
            """,
            (content_row_id,),
        )
        return tuple(self._tag_assertion_from_row(row, content_id) for row in rows)

    def content_for_path(self, root: Path, relative_path: PurePosixPath) -> ContentId:
        """Resolve a current relative path to its content identity."""
        return self._content_for_current_path(root, relative_path)

    def tags_for_path(self, root: Path, relative_path: PurePosixPath) -> tuple[ContentTagAssertion, ...]:
        """Return active content tags resolved through a current relative path."""
        return self.tags_for_content(self._content_for_current_path(root, relative_path))

    def search_available_tags(
        self,
        *,
        name_regex: str | None = None,
        provenance: TagProvenanceKind | None = None,
        sort_by: AvailableTagSort = "name",
        reverse: bool = False,
        limit: int = 20,
    ) -> AvailableTagSearch:
        """Return bounded catalog-wide active tag usage counts."""
        compiled_regex = None if name_regex is None else re.compile(name_regex)
        if provenance not in (None, "user", "system"):
            raise ValueError("provenance must be 'user', 'system', or None")
        if sort_by not in ("name", "content", "assertions"):
            raise ValueError(f"unsupported available-tag sort: {sort_by}")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        provenance_clause = "" if provenance is None else "AND assertions.provenance_kind = ?"
        parameters: tuple[object, ...] = () if provenance is None else (provenance,)
        regex_clause = "" if compiled_regex is None else f"WHERE {_TAG_NAME_REGEX_FUNCTION}(tag_name)"
        usage_cte = f"""
            WITH tag_usage AS (
                SELECT
                    tags.name AS tag_name,
                    COUNT(DISTINCT assertions.content_id) AS content_count,
                    COUNT(*) AS assertion_count,
                    SUM(assertions.provenance_kind = 'user') AS user_assertion_count,
                    SUM(assertions.provenance_kind = 'system') AS system_assertion_count
                FROM content_tag_assertions AS assertions
                JOIN tags ON tags.id = assertions.tag_id
                WHERE assertions.retracted_at_ns IS NULL
                  {provenance_clause}
                GROUP BY tags.id, tags.name
            )
        """
        order_column = {
            "name": "tag_name",
            "content": "content_count",
            "assertions": "assertion_count",
        }[sort_by]
        default_direction = "ASC" if sort_by == "name" else "DESC"
        direction = self._reverse_direction(default_direction) if reverse else default_direction
        tie_breaker = "" if sort_by == "name" else ", tag_name ASC"

        if compiled_regex is not None:

            def matches_tag_name(tag_name: str) -> int:
                return int(compiled_regex.search(tag_name) is not None)

            self._connection.create_function(
                _TAG_NAME_REGEX_FUNCTION,
                1,
                matches_tag_name,
                deterministic=True,
            )
        try:
            count_row = self._connection.execute(
                f"""
                {usage_cte}
                SELECT COUNT(*) AS total_matches
                FROM tag_usage
                {regex_clause}
                """,
                parameters,
            ).fetchone()
            assert count_row is not None
            rows = self._connection.execute(
                f"""
                {usage_cte}
                SELECT
                    tag_name,
                    content_count,
                    assertion_count,
                    user_assertion_count,
                    system_assertion_count
                FROM tag_usage
                {regex_clause}
                ORDER BY {order_column} {direction}{tie_breaker}
                LIMIT ?
                """,
                (*parameters, limit),
            )
            tags = tuple(self._tag_usage_from_row(row) for row in rows)
        finally:
            if compiled_regex is not None:
                self._connection.create_function(_TAG_NAME_REGEX_FUNCTION, 1, None)
        return AvailableTagSearch(tags=tags, total_matches=int(count_row["total_matches"]))

    def search_tagged_content(
        self,
        tag: str,
        *,
        provenance: TagProvenanceKind | None = None,
        limit: int = 20,
    ) -> TaggedContentSearch:
        """Return bounded content identities with one active tag assertion."""
        canonical_tag = validate_tag_name(tag)
        if provenance not in (None, "user", "system"):
            raise ValueError("provenance must be 'user', 'system', or None")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        provenance_clause = "" if provenance is None else "AND assertions.provenance_kind = ?"
        parameters: list[object] = [canonical_tag]
        if provenance is not None:
            parameters.append(provenance)
        count_row = self._connection.execute(
            f"""
            SELECT COUNT(DISTINCT assertions.content_id) AS total_matches
            FROM content_tag_assertions AS assertions
            JOIN tags ON tags.id = assertions.tag_id
            WHERE tags.name = ? AND assertions.retracted_at_ns IS NULL {provenance_clause}
            """,
            parameters,
        ).fetchone()
        assert count_row is not None
        content_rows = tuple(
            self._connection.execute(
                f"""
                SELECT DISTINCT
                    content.id AS content_row_id,
                    content.algorithm AS algorithm,
                    content.digest AS digest,
                    content.size_bytes AS size_bytes
                FROM content
                JOIN content_tag_assertions AS assertions ON assertions.content_id = content.id
                JOIN tags ON tags.id = assertions.tag_id
                WHERE tags.name = ? AND assertions.retracted_at_ns IS NULL {provenance_clause}
                ORDER BY content.algorithm, content.digest
                LIMIT ?
                """,
                (*parameters, limit),
            )
        )
        if not content_rows:
            return TaggedContentSearch(contents=(), total_matches=int(count_row["total_matches"]))
        content_ids = {
            int(row["content_row_id"]): ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"]))
            for row in content_rows
        }
        placeholders = ", ".join("?" for _ in content_rows)
        assertion_parameters: list[object] = [canonical_tag, *content_ids]
        assertion_provenance_clause = ""
        if provenance is not None:
            assertion_provenance_clause = "AND assertions.provenance_kind = ?"
            assertion_parameters.append(provenance)
        assertion_rows = self._connection.execute(
            f"""
            SELECT
                assertions.content_id AS content_row_id,
                tags.name AS tag_name,
                assertions.provenance_kind AS provenance_kind,
                assertions.source_name AS source_name,
                assertions.source_version AS source_version,
                assertions.source_detail AS source_detail,
                assertions.asserted_at_ns AS asserted_at_ns
            FROM content_tag_assertions AS assertions
            JOIN tags ON tags.id = assertions.tag_id
            WHERE tags.name = ?
              AND assertions.content_id IN ({placeholders})
              AND assertions.retracted_at_ns IS NULL
              {assertion_provenance_clause}
            ORDER BY
                assertions.content_id,
                assertions.provenance_kind,
                assertions.source_name,
                assertions.source_version,
                assertions.source_detail,
                assertions.asserted_at_ns
            """,
            assertion_parameters,
        )
        assertions_by_content: dict[int, list[ContentTagAssertion]] = {row_id: [] for row_id in content_ids}
        for row in assertion_rows:
            row_id = int(row["content_row_id"])
            assertions_by_content[row_id].append(self._tag_assertion_from_row(row, content_ids[row_id]))
        contents = tuple(
            TaggedContent(
                content_id=content_ids[int(row["content_row_id"])],
                size_bytes=int(row["size_bytes"]),
                assertions=tuple(assertions_by_content[int(row["content_row_id"])]),
            )
            for row in content_rows
        )
        return TaggedContentSearch(contents=contents, total_matches=int(count_row["total_matches"]))

    def search_content_by_tags(
        self,
        root: Path,
        tags: Collection[str],
        *,
        match: TagMatchMode = "all",
        provenance: TagProvenanceKind | None = None,
        limit: int = 20,
        path_limit: int = 20,
        tag_limit: int | None = 3,
    ) -> MultiTagContentSearch:
        """Return bounded content matching active tags with root-scoped path previews."""
        canonical_tags = tuple(dict.fromkeys(validate_tag_name(tag) for tag in tags))
        if not canonical_tags:
            raise ValueError("at least one tag is required")
        if match not in ("all", "any"):
            raise ValueError("match must be 'all' or 'any'")
        if provenance not in (None, "user", "system"):
            raise ValueError("provenance must be 'user', 'system', or None")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if path_limit < 1:
            raise ValueError("path_limit must be at least 1")
        if tag_limit is not None and tag_limit < 1:
            raise ValueError("tag_limit must be at least 1 or None")

        location = self._location_for_root(root)
        location_id = None if location is None else location.id
        tag_placeholders = ", ".join("?" for _ in canonical_tags)
        provenance_clause = "" if provenance is None else "AND assertions.provenance_kind = ?"
        matching_parameters: list[object] = list(canonical_tags)
        if provenance is not None:
            matching_parameters.append(provenance)
        matching_parameters.append(len(canonical_tags) if match == "all" else 1)
        matching_content_cte = f"""
            matching_content AS (
                SELECT assertions.content_id
                FROM content_tag_assertions AS assertions
                JOIN tags ON tags.id = assertions.tag_id
                WHERE assertions.retracted_at_ns IS NULL
                  AND tags.name IN ({tag_placeholders})
                  {provenance_clause}
                GROUP BY assertions.content_id
                HAVING COUNT(DISTINCT tags.name) >= ?
            )
        """

        total_row = self._connection.execute(
            f"""
            WITH {matching_content_cte}
            SELECT
                COUNT(DISTINCT matching_content.content_id) AS total_matches,
                COUNT(observations.id) AS total_current_paths
            FROM matching_content
            LEFT JOIN locations ON locations.id = ?
            LEFT JOIN file_observations AS observations
              ON observations.scan_id = locations.current_scan_id
             AND observations.content_id = matching_content.content_id
            """,
            (*matching_parameters, location_id),
        ).fetchone()
        assert total_row is not None

        content_rows = tuple(
            self._connection.execute(
                f"""
                WITH {matching_content_cte}
                SELECT
                    content.id AS content_row_id,
                    content.algorithm AS algorithm,
                    content.digest AS digest,
                    content.size_bytes AS size_bytes
                FROM matching_content
                JOIN content ON content.id = matching_content.content_id
                ORDER BY content.algorithm, content.digest
                LIMIT ?
                """,
                (*matching_parameters, limit),
            )
        )
        if not content_rows:
            return MultiTagContentSearch(
                contents=(),
                total_matches=int(total_row["total_matches"]),
                total_current_paths=int(total_row["total_current_paths"]),
            )

        content_ids = {
            int(row["content_row_id"]): ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"]))
            for row in content_rows
        }
        content_row_ids = tuple(content_ids)
        content_placeholders = ", ".join("?" for _ in content_row_ids)
        paths_by_content: dict[int, list[PurePosixPath]] = {row_id: [] for row_id in content_row_ids}
        path_counts = {row_id: 0 for row_id in content_row_ids}
        if location_id is not None:
            path_rows = self._connection.execute(
                f"""
                WITH ranked_paths AS (
                    SELECT
                        observations.content_id AS content_row_id,
                        observations.relative_path AS relative_path,
                        COUNT(*) OVER (PARTITION BY observations.content_id) AS current_path_count,
                        ROW_NUMBER() OVER (
                            PARTITION BY observations.content_id
                            ORDER BY observations.relative_path
                        ) AS path_rank
                    FROM locations
                    JOIN file_observations AS observations
                      ON observations.scan_id = locations.current_scan_id
                    WHERE locations.id = ?
                      AND observations.content_id IN ({content_placeholders})
                )
                SELECT content_row_id, relative_path, current_path_count
                FROM ranked_paths
                WHERE path_rank <= ?
                ORDER BY content_row_id, relative_path
                """,
                (location_id, *content_row_ids, path_limit),
            )
            for row in path_rows:
                row_id = int(row["content_row_id"])
                path_counts[row_id] = int(row["current_path_count"])
                paths_by_content[row_id].append(PurePosixPath(str(row["relative_path"])))

        tag_limit_clause = "" if tag_limit is None else "WHERE tag_rank <= ?"
        tag_parameters: tuple[object, ...] = content_row_ids if tag_limit is None else (*content_row_ids, tag_limit)
        tag_rows = self._connection.execute(
            f"""
            WITH distinct_active_tags AS (
                SELECT DISTINCT assertions.content_id AS content_row_id, tags.name AS tag_name
                FROM content_tag_assertions AS assertions
                JOIN tags ON tags.id = assertions.tag_id
                WHERE assertions.retracted_at_ns IS NULL
                  AND assertions.content_id IN ({content_placeholders})
            ),
            ranked_tags AS (
                SELECT
                    content_row_id,
                    tag_name,
                    COUNT(*) OVER (PARTITION BY content_row_id) AS active_tag_count,
                    ROW_NUMBER() OVER (PARTITION BY content_row_id ORDER BY tag_name) AS tag_rank
                FROM distinct_active_tags
            )
            SELECT content_row_id, tag_name, active_tag_count
            FROM ranked_tags
            {tag_limit_clause}
            ORDER BY content_row_id, tag_name
            """,
            tag_parameters,
        )
        tags_by_content: dict[int, list[str]] = {row_id: [] for row_id in content_row_ids}
        tag_counts = {row_id: 0 for row_id in content_row_ids}
        for row in tag_rows:
            row_id = int(row["content_row_id"])
            tag_counts[row_id] = int(row["active_tag_count"])
            tags_by_content[row_id].append(str(row["tag_name"]))

        contents = tuple(
            ContentTagView(
                content_id=content_ids[int(row["content_row_id"])],
                size_bytes=int(row["size_bytes"]),
                current_path_count=path_counts[int(row["content_row_id"])],
                current_paths=tuple(paths_by_content[int(row["content_row_id"])]),
                active_tag_count=tag_counts[int(row["content_row_id"])],
                tags=tuple(tags_by_content[int(row["content_row_id"])]),
            )
            for row in content_rows
        )
        return MultiTagContentSearch(
            contents=contents,
            total_matches=int(total_row["total_matches"]),
            total_current_paths=int(total_row["total_current_paths"]),
        )

    def current_files(self, root: Path) -> list[FileObservation]:
        """Return the current files for one location in relative-path order."""
        location = self._location_for_root(root)
        if location is None:
            return []
        return self._observations_for_query(
            """
            WHERE scans.location_id = ? AND scans.id = locations.current_scan_id
            ORDER BY observations.relative_path
            """,
            (location.id,),
        )

    def search_current_files(
        self,
        root: Path,
        *,
        path_glob: str | None = None,
        sort_by: CurrentFileSort = "path",
        reverse: bool = False,
        limit: int = 20,
    ) -> CurrentFileSearch:
        """Return a bounded, deterministically ordered query over one location's current files."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        location = self._location_for_root(root)
        if location is None:
            return CurrentFileSearch(files=(), total_matches=0, total_size_bytes=0)

        order_column, default_direction = self._current_file_order(sort_by)
        direction = self._reverse_direction(default_direction) if reverse else default_direction
        where_clause = "WHERE scans.location_id = ? AND scans.id = locations.current_scan_id"
        parameters: list[object] = [location.id]
        if path_glob is not None:
            where_clause += " AND observations.relative_path GLOB ?"
            parameters.append(path_glob)

        count_row = self._connection.execute(
            f"""
            SELECT COUNT(*) AS total_matches, COALESCE(SUM(observations.size_bytes), 0) AS total_size_bytes
            FROM file_observations AS observations
            JOIN scan_runs AS scans ON scans.id = observations.scan_id
            JOIN locations ON locations.id = scans.location_id
            {where_clause}
            """,
            parameters,
        ).fetchone()
        assert count_row is not None
        rows = self._connection.execute(
            f"""
            SELECT
                observations.relative_path AS relative_path,
                observations.size_bytes AS size_bytes,
                observations.mtime_ns AS mtime_ns,
                content.algorithm AS algorithm,
                content.digest AS digest
            FROM file_observations AS observations
            JOIN scan_runs AS scans ON scans.id = observations.scan_id
            JOIN locations ON locations.id = scans.location_id
            JOIN content ON content.id = observations.content_id
            {where_clause}
            ORDER BY {order_column} {direction}, observations.relative_path ASC
            LIMIT ?
            """,
            (*parameters, limit),
        )
        files = tuple(
            FileObservation(
                location=location,
                relative_path=PurePosixPath(str(row["relative_path"])),
                content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
                size_bytes=int(row["size_bytes"]),
                mtime_ns=int(row["mtime_ns"]),
            )
            for row in rows
        )
        return CurrentFileSearch(
            files=files,
            total_matches=int(count_row["total_matches"]),
            total_size_bytes=int(count_row["total_size_bytes"]),
        )

    def search_current_files_with_tags(
        self,
        root: Path,
        *,
        path_glob: str | None = None,
        tags: Collection[str] = (),
        match: TagMatchMode = "all",
        provenance: TagProvenanceKind | None = None,
        sort_by: CurrentFileSort = "path",
        reverse: bool = False,
        limit: int = 20,
        tag_limit: int | None = 3,
    ) -> TagAwareCurrentFileSearch:
        """Return bounded current files with tag filtering and active-tag previews."""
        canonical_tags = tuple(dict.fromkeys(validate_tag_name(tag) for tag in tags))
        if match not in ("all", "any"):
            raise ValueError("match must be 'all' or 'any'")
        if provenance not in (None, "user", "system"):
            raise ValueError("provenance must be 'user', 'system', or None")
        if provenance is not None and not canonical_tags:
            raise ValueError("provenance requires at least one tag")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if tag_limit is not None and tag_limit < 1:
            raise ValueError("tag_limit must be at least 1 or None")

        order_column, default_direction = self._current_file_order(sort_by)
        direction = self._reverse_direction(default_direction) if reverse else default_direction
        location = self._location_for_root(root)
        if location is None:
            return TagAwareCurrentFileSearch(
                files=(),
                total_file_count=0,
                total_content_count=0,
                total_file_size_bytes=0,
            )

        matching_content_cte = ""
        matching_content_clause = ""
        matching_parameters: list[object] = []
        if canonical_tags:
            tag_placeholders = ", ".join("?" for _ in canonical_tags)
            provenance_clause = "" if provenance is None else "AND assertions.provenance_kind = ?"
            matching_parameters.extend(canonical_tags)
            if provenance is not None:
                matching_parameters.append(provenance)
            matching_parameters.append(len(canonical_tags) if match == "all" else 1)
            matching_content_cte = f"""
                WITH matching_content AS (
                    SELECT assertions.content_id
                    FROM content_tag_assertions AS assertions
                    JOIN tags ON tags.id = assertions.tag_id
                    WHERE assertions.retracted_at_ns IS NULL
                      AND tags.name IN ({tag_placeholders})
                      {provenance_clause}
                    GROUP BY assertions.content_id
                    HAVING COUNT(DISTINCT tags.name) >= ?
                )
            """
            matching_content_clause = " AND observations.content_id IN (SELECT content_id FROM matching_content)"

        where_clause = f"WHERE scans.location_id = ? AND scans.id = locations.current_scan_id{matching_content_clause}"
        parameters: list[object] = [*matching_parameters, location.id]
        if path_glob is not None:
            where_clause += " AND observations.relative_path GLOB ?"
            parameters.append(path_glob)

        count_row = self._connection.execute(
            f"""
            {matching_content_cte}
            SELECT
                COUNT(*) AS total_file_count,
                COUNT(DISTINCT observations.content_id) AS total_content_count,
                COALESCE(SUM(observations.size_bytes), 0) AS total_file_size_bytes
            FROM file_observations AS observations
            JOIN scan_runs AS scans ON scans.id = observations.scan_id
            JOIN locations ON locations.id = scans.location_id
            {where_clause}
            """,
            parameters,
        ).fetchone()
        assert count_row is not None

        rows = tuple(
            self._connection.execute(
                f"""
                {matching_content_cte}
                SELECT
                    observations.relative_path AS relative_path,
                    observations.size_bytes AS size_bytes,
                    observations.mtime_ns AS mtime_ns,
                    content.id AS content_row_id,
                    content.algorithm AS algorithm,
                    content.digest AS digest
                FROM file_observations AS observations
                JOIN scan_runs AS scans ON scans.id = observations.scan_id
                JOIN locations ON locations.id = scans.location_id
                JOIN content ON content.id = observations.content_id
                {where_clause}
                ORDER BY {order_column} {direction}, observations.relative_path ASC
                LIMIT ?
                """,
                (*parameters, limit),
            )
        )
        if not rows:
            return TagAwareCurrentFileSearch(
                files=(),
                total_file_count=int(count_row["total_file_count"]),
                total_content_count=int(count_row["total_content_count"]),
                total_file_size_bytes=int(count_row["total_file_size_bytes"]),
            )

        content_row_ids = tuple(dict.fromkeys(int(row["content_row_id"]) for row in rows))
        content_placeholders = ", ".join("?" for _ in content_row_ids)
        tag_limit_clause = "" if tag_limit is None else "WHERE tag_rank <= ?"
        tag_parameters: tuple[object, ...] = content_row_ids if tag_limit is None else (*content_row_ids, tag_limit)
        tag_rows = self._connection.execute(
            f"""
            WITH distinct_active_tags AS (
                SELECT DISTINCT assertions.content_id AS content_row_id, tags.name AS tag_name
                FROM content_tag_assertions AS assertions
                JOIN tags ON tags.id = assertions.tag_id
                WHERE assertions.retracted_at_ns IS NULL
                  AND assertions.content_id IN ({content_placeholders})
            ),
            ranked_tags AS (
                SELECT
                    content_row_id,
                    tag_name,
                    COUNT(*) OVER (PARTITION BY content_row_id) AS active_tag_count,
                    ROW_NUMBER() OVER (PARTITION BY content_row_id ORDER BY tag_name) AS tag_rank
                FROM distinct_active_tags
            )
            SELECT content_row_id, tag_name, active_tag_count
            FROM ranked_tags
            {tag_limit_clause}
            ORDER BY content_row_id, tag_name
            """,
            tag_parameters,
        )
        tags_by_content: dict[int, list[str]] = {row_id: [] for row_id in content_row_ids}
        tag_counts = {row_id: 0 for row_id in content_row_ids}
        for row in tag_rows:
            row_id = int(row["content_row_id"])
            tag_counts[row_id] = int(row["active_tag_count"])
            tags_by_content[row_id].append(str(row["tag_name"]))

        files = tuple(
            CurrentFileTagView(
                observation=FileObservation(
                    location=location,
                    relative_path=PurePosixPath(str(row["relative_path"])),
                    content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
                    size_bytes=int(row["size_bytes"]),
                    mtime_ns=int(row["mtime_ns"]),
                ),
                active_tag_count=tag_counts[int(row["content_row_id"])],
                tags=tuple(tags_by_content[int(row["content_row_id"])]),
            )
            for row in rows
        )
        return TagAwareCurrentFileSearch(
            files=files,
            total_file_count=int(count_row["total_file_count"]),
            total_content_count=int(count_row["total_content_count"]),
            total_file_size_bytes=int(count_row["total_file_size_bytes"]),
        )

    def find_by_content(self, root: Path, content_id: ContentId) -> list[FileObservation]:
        """Return current observations at one location with a supplied content identity."""
        location = self._location_for_root(root)
        if location is None:
            return []
        return self._observations_for_query(
            """
            WHERE scans.location_id = ? AND scans.id = locations.current_scan_id
              AND content.algorithm = ? AND content.digest = ?
            ORDER BY observations.relative_path
            """,
            (location.id, content_id.algorithm, content_id.digest),
        )

    def duplicate_groups(self, root: Path) -> list[tuple[FileObservation, ...]]:
        """Return current duplicate-content groups at one location, deterministically ordered."""
        location = self._location_for_root(root)
        if location is None:
            return []
        # Equal content IDs must be contiguous so the loop below can form groups in one pass.
        observations = self._observations_for_query(
            """
            WHERE scans.location_id = ? AND scans.id = locations.current_scan_id
            ORDER BY content.algorithm, content.digest, observations.relative_path
            """,
            (location.id,),
        )
        groups: list[tuple[FileObservation, ...]] = []
        current_group: list[FileObservation] = []
        current_content: ContentId | None = None
        for observation in observations:
            if observation.content_id != current_content:
                if len(current_group) > 1:
                    groups.append(tuple(current_group))
                current_group = []
                current_content = observation.content_id
            current_group.append(observation)
        if len(current_group) > 1:
            groups.append(tuple(current_group))
        return groups

    def current_scan(self, root: Path) -> ScanRun | None:
        """Return the most recent successful scan for one location, if one exists."""
        location = self._location_for_root(root)
        if location is None:
            return None
        row = self._connection.execute(
            """
            SELECT
                scans.id AS scan_id,
                locations.id AS location_id,
                locations.root_path AS root_path,
                scans.status AS scan_status,
                scans.started_at_ns AS started_at_ns,
                scans.completed_at_ns AS completed_at_ns
            FROM locations
            JOIN scan_runs AS scans ON scans.id = locations.current_scan_id
            WHERE locations.id = ?
            """,
            (location.id,),
        ).fetchone()
        return None if row is None else self._scan_run_from_row(row)

    def current_summary(self, root: Path) -> ScanSummary | None:
        """Return the summary for the current successful scan, if one exists."""
        current_scan = self.current_scan(root)
        return None if current_scan is None else self._scan_summary(current_scan.id)

    def duplicate_summary(self, root: Path) -> DuplicateSummary:
        """Return aggregate duplicate metrics for a location's current successful scan."""
        current_scan = self.current_scan(root)
        if current_scan is None:
            return DuplicateSummary(
                duplicate_content_group_count=0,
                duplicate_file_instance_count=0,
                potential_redundant_bytes=0,
            )
        row = self._connection.execute(
            """
            SELECT
                COUNT(*) AS duplicate_content_group_count,
                COALESCE(SUM(copy_count), 0) AS duplicate_file_instance_count,
                COALESCE(SUM((copy_count - 1) * size_bytes), 0) AS potential_redundant_bytes
            FROM (
                SELECT content_id, MAX(size_bytes) AS size_bytes, COUNT(*) AS copy_count
                FROM file_observations
                WHERE scan_id = ?
                GROUP BY content_id
                HAVING COUNT(*) > 1
            )
            """,
            (current_scan.id,),
        ).fetchone()
        assert row is not None
        return DuplicateSummary(
            duplicate_content_group_count=int(row["duplicate_content_group_count"]),
            duplicate_file_instance_count=int(row["duplicate_file_instance_count"]),
            potential_redundant_bytes=int(row["potential_redundant_bytes"]),
        )

    def search_duplicate_groups(
        self,
        root: Path,
        *,
        group_limit: int = 20,
        member_limit: int = 20,
    ) -> DuplicateGroupSearch:
        """Return bounded duplicate groups and complete aggregate metrics."""
        if group_limit < 1:
            raise ValueError("group_limit must be at least 1")
        if member_limit < 1:
            raise ValueError("member_limit must be at least 1")
        location = self._location_for_root(root)
        if location is None:
            return DuplicateGroupSearch(groups=(), summary=DuplicateSummary(0, 0, 0))
        current_scan = self.current_scan(root)
        if current_scan is None:
            return DuplicateGroupSearch(groups=(), summary=DuplicateSummary(0, 0, 0))

        summary = self.duplicate_summary(root)
        group_rows = self._connection.execute(
            """
            SELECT
                content.id AS content_database_id,
                content.algorithm AS algorithm,
                content.digest AS digest,
                content.size_bytes AS size_bytes,
                COUNT(*) AS file_instance_count,
                (COUNT(*) - 1) * content.size_bytes AS potential_redundant_bytes
            FROM file_observations AS observations
            JOIN content ON content.id = observations.content_id
            WHERE observations.scan_id = ?
            GROUP BY content.id, content.algorithm, content.digest, content.size_bytes
            HAVING COUNT(*) > 1
            ORDER BY potential_redundant_bytes DESC, content.algorithm, content.digest
            LIMIT ?
            """,
            (current_scan.id, group_limit),
        ).fetchall()
        groups = tuple(
            DuplicateGroupView(
                content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
                size_bytes=int(row["size_bytes"]),
                file_instance_count=int(row["file_instance_count"]),
                potential_redundant_bytes=int(row["potential_redundant_bytes"]),
                members=tuple(
                    self._observations_for_query(
                        """
                        WHERE scans.id = ? AND observations.content_id = ?
                        ORDER BY observations.relative_path
                        LIMIT ?
                        """,
                        (current_scan.id, int(row["content_database_id"]), member_limit),
                    )
                ),
            )
            for row in group_rows
        )
        return DuplicateGroupSearch(groups=groups, summary=summary)

    def scan_history(self) -> Iterator[ScanRun]:
        """Yield every scan run in deterministic location-root and scan-ID order.

        The catalog must remain open while consuming the iterator.
        """
        cursor = self._connection.execute(
            """
            SELECT
                scans.id AS scan_id,
                locations.id AS location_id,
                locations.root_path AS root_path,
                scans.status AS scan_status,
                scans.started_at_ns AS started_at_ns,
                scans.completed_at_ns AS completed_at_ns
            FROM scan_runs AS scans
            JOIN locations ON locations.id = scans.location_id
            ORDER BY locations.root_path, scans.id
            """
        )
        for row in cursor:
            yield self._scan_run_from_row(row)

    def observation_history(self) -> Iterator[HistoricalObservation]:
        """Yield every persisted file observation in deterministic history order.

        The catalog must remain open while consuming the iterator.
        """
        yield from self._historical_observations_for_query(
            "ORDER BY locations.root_path, scans.id, observations.relative_path",
            (),
        )

    def _content_record(self, content_id: ContentId) -> tuple[int, int]:
        row = self._connection.execute(
            "SELECT id, size_bytes FROM content WHERE algorithm = ? AND digest = ?",
            (content_id.algorithm, content_id.digest),
        ).fetchone()
        if row is None:
            raise TaggingError(f"content is not known to this catalog: {content_id.algorithm}:{content_id.digest}")
        return int(row["id"]), int(row["size_bytes"])

    def _content_for_current_path(self, root: Path, relative_path: PurePosixPath) -> ContentId:
        canonical_path = self._validated_relative_path(relative_path)
        location = self._location_for_root(root)
        if location is None:
            raise TaggingError(f"catalog has no current path: {canonical_path.as_posix()}")
        row = self._connection.execute(
            """
            SELECT content.algorithm AS algorithm, content.digest AS digest
            FROM file_observations AS observations
            JOIN scan_runs AS scans ON scans.id = observations.scan_id
            JOIN locations ON locations.id = scans.location_id
            JOIN content ON content.id = observations.content_id
            WHERE locations.id = ?
              AND scans.id = locations.current_scan_id
              AND observations.relative_path = ?
            """,
            (location.id, canonical_path.as_posix()),
        ).fetchone()
        if row is None:
            raise TaggingError(f"catalog has no current path: {canonical_path.as_posix()}")
        return ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"]))

    @staticmethod
    def _validated_relative_path(relative_path: PurePosixPath) -> PurePosixPath:
        text = relative_path.as_posix()
        if relative_path.is_absolute() or text in ("", ".") or ".." in relative_path.parts or "\\" in text:
            raise ValueError("tag path must be a non-empty POSIX relative path without parent traversal")
        return relative_path

    @staticmethod
    def _tag_usage_from_row(row: sqlite3.Row) -> TagUsage:
        return TagUsage(
            tag=str(row["tag_name"]),
            content_count=int(row["content_count"]),
            assertion_count=int(row["assertion_count"]),
            user_assertion_count=int(row["user_assertion_count"]),
            system_assertion_count=int(row["system_assertion_count"]),
        )

    @staticmethod
    def _tag_assertion_from_row(row: sqlite3.Row, content_id: ContentId) -> ContentTagAssertion:
        return ContentTagAssertion(
            content_id=content_id,
            tag=str(row["tag_name"]),
            provenance=TagProvenance(
                kind=cast(TagProvenanceKind, str(row["provenance_kind"])),
                source_name=str(row["source_name"]),
                source_version=str(row["source_version"]),
                source_detail=str(row["source_detail"]),
            ),
            asserted_at_ns=int(row["asserted_at_ns"]),
        )

    @staticmethod
    def _current_file_order(sort_by: CurrentFileSort) -> tuple[str, str]:
        if sort_by == "path":
            return "observations.relative_path", "ASC"
        if sort_by == "size":
            return "observations.size_bytes", "DESC"
        if sort_by == "date":
            return "observations.mtime_ns", "DESC"
        raise ValueError(f"unsupported current-file sort: {sort_by}")

    @staticmethod
    def _reverse_direction(direction: str) -> str:
        return "DESC" if direction == "ASC" else "ASC"

    def _canonical_root(self, root: Path) -> Path:
        if root.is_symlink():
            raise ScanFailure("a scan root must not be a symbolic link")
        if not root.is_dir():
            raise ScanFailure(f"scan root is not a directory: {root}")
        return root.resolve(strict=True)

    def _location_for_root(self, root: Path) -> Location | None:
        canonical_root = self._canonical_root(root)
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
        self, path: Path, location: Location, previous: FileObservation
    ) -> tuple[FileObservation, bool]:
        if path.is_symlink() or not path.is_file():
            raise RefreshFailure(f"file changed type during reconciliation: {path}")
        metadata = path.stat()
        if metadata.st_size == previous.size_bytes and metadata.st_mtime_ns == previous.mtime_ns:
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
        row = self._connection.execute(
            """
            SELECT
                COUNT(*) AS files_observed,
                COALESCE(SUM(size_bytes), 0) AS total_bytes_observed,
                COUNT(DISTINCT content_id) AS distinct_content_count,
                COUNT(DISTINCT CASE WHEN duplicate_group = 1 THEN content_id END)
                    AS duplicate_content_group_count
            FROM (
                SELECT observations.*, CASE WHEN COUNT(*) OVER (PARTITION BY content_id) > 1
                    THEN 1 ELSE 0 END AS duplicate_group
                FROM file_observations AS observations
                WHERE scan_id = ?
            )
            """,
            (scan_id,),
        ).fetchone()
        assert row is not None
        return ScanSummary(
            files_observed=int(row["files_observed"]),
            total_bytes_observed=int(row["total_bytes_observed"]),
            distinct_content_count=int(row["distinct_content_count"]),
            duplicate_content_group_count=int(row["duplicate_content_group_count"]),
        )

    def _current_observation_iterator(self, location: Location) -> Iterator[FileObservation]:
        """Stream one location's current observations in relative-path order."""
        assert location.id is not None
        cursor = self._connection.execute(
            """
            SELECT observations.relative_path, observations.size_bytes, observations.mtime_ns,
                   content.algorithm, content.digest
            FROM file_observations AS observations
            JOIN content ON content.id = observations.content_id
            JOIN locations ON locations.current_scan_id = observations.scan_id
            WHERE locations.id = ?
            ORDER BY observations.relative_path
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

    def _observations_for_query(self, where_clause: str, parameters: tuple[object, ...]) -> list[FileObservation]:
        """Return a filtered projection of the shared historical observation query."""
        return [
            FileObservation(
                location=observation.scan.location,
                relative_path=observation.relative_path,
                content_id=observation.content_id,
                size_bytes=observation.size_bytes,
                mtime_ns=observation.mtime_ns,
            )
            for observation in self._historical_observations_for_query(where_clause, parameters)
        ]

    def _historical_observations_for_query(
        self, where_clause: str, parameters: tuple[object, ...]
    ) -> Iterator[HistoricalObservation]:
        """Yield typed rows from the shared observation/scan/location/content join."""
        cursor = self._connection.execute(
            f"""
            SELECT
                scans.id AS scan_id,
                locations.id AS location_id,
                locations.root_path AS root_path,
                scans.status AS scan_status,
                scans.started_at_ns AS started_at_ns,
                scans.completed_at_ns AS completed_at_ns,
                observations.relative_path AS relative_path,
                observations.size_bytes AS size_bytes,
                observations.mtime_ns AS mtime_ns,
                content.algorithm AS algorithm,
                content.digest AS digest
            FROM file_observations AS observations
            JOIN scan_runs AS scans ON scans.id = observations.scan_id
            JOIN locations ON locations.id = scans.location_id
            JOIN content ON content.id = observations.content_id
            {where_clause}
            """,
            parameters,
        )
        for row in cursor:
            yield HistoricalObservation(
                scan=self._scan_run_from_row(row),
                relative_path=PurePosixPath(str(row["relative_path"])),
                content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
                size_bytes=int(row["size_bytes"]),
                mtime_ns=int(row["mtime_ns"]),
            )

    @staticmethod
    def _scan_run_from_row(row: sqlite3.Row) -> ScanRun:
        """Build a scan domain object from a joined scan/location row."""
        completed_at_ns = row["completed_at_ns"]
        return ScanRun(
            id=int(row["scan_id"]),
            location=Location(id=int(row["location_id"]), root=Path(str(row["root_path"]))),
            status=str(row["scan_status"]),
            started_at_ns=int(row["started_at_ns"]),
            completed_at_ns=None if completed_at_ns is None else int(completed_at_ns),
        )
