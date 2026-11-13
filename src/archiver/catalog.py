"""SQLite persistence and read-only local directory scanning."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Collection, Iterator
from contextlib import closing, suppress
from dataclasses import asdict
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
    iter_scan_summaries,
)
from .comparison import ContentComparison
from .errors import (
    HistoryPruningError,
    InvalidCatalogError,
    PrunedScanError,
    RefreshFailure,
    ScanFailure,
    StaleRefreshError,
    TaggingError,
)
from .filesystem import regular_files, targeted_path
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
    InventoryChanges,
    InventoryRevision,
    InventoryUpdateResult,
    Location,
    PruneHistorySummary,
    RefreshChange,
    RefreshChangeKind,
    RefreshChangeSet,
    ScanHistorySummary,
    ScanProgress,
    ScanRun,
    ScanSummary,
    TagProvenance,
    TagUsage,
    validate_tag_name,
)
from .operations import (
    OPERATION_SCHEMA,
    FinalizationRequest,
    OperationCounts,
    OperationError,
    OperationRecord,
    OperationScope,
    OperationSpec,
    canonical,
    decode_revision,
    positive,
    prepare_operation_request,
    revision_data,
    safe_root,
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

SCHEMA_VERSION = 4
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
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    origin TEXT NOT NULL DEFAULT 'full_scan' CHECK (origin IN ('full_scan', 'targeted_update'))
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

_SCHEMA += OPERATION_SCHEMA

_BASE_SCHEMA_TABLES = frozenset({"catalog_metadata", "locations", "content", "scan_runs", "file_observations"})
_TAG_SCHEMA_TABLES = frozenset({"tags", "content_tag_assertions"})

# Selection depends only on scan rows, so it remains stable after deleting the
# children. Retaining the newest successful scans also preserves the maximum ID.
_HISTORY_RETENTION_CTE = """
WITH retained AS (
    SELECT id FROM scan_runs
    WHERE location_id = :location_id AND status = 'completed'
    ORDER BY id DESC LIMIT :keep_last
), candidates AS (
    SELECT id, status FROM scan_runs
    WHERE location_id = :location_id
      AND ((status = 'completed' AND id NOT IN (SELECT id FROM retained))
           OR (:include_failed AND status = 'failed'
               AND id < (SELECT MIN(id) FROM retained)))
)
"""


def _compare_relative_path_components(left: str, right: str) -> int:
    """Match depth-first traversal and PurePosixPath merge comparisons."""
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    return (left_parts > right_parts) - (left_parts < right_parts)


def _validate_schema_tables(connection: sqlite3.Connection, version: int, *, schema: str = "main") -> None:
    if schema not in ("main", "source_catalog", "target_catalog"):
        raise ValueError("invalid internal schema alias")
    rows = connection.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")
    actual = {str(row[0]) for row in rows}
    required = _BASE_SCHEMA_TABLES | (_TAG_SCHEMA_TABLES if version >= 3 else frozenset())
    if version == 4:
        required |= {"operations"}
    missing = sorted(required - actual)
    if version >= 3:
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA {schema}.table_info(scan_runs)")}
        if "origin" not in columns:
            raise InvalidCatalogError("catalog schema is missing scan_runs.origin")
    if missing:
        raise InvalidCatalogError(f"catalog schema is missing required tables: {', '.join(missing)}")
    if version >= 3:
        for table, fields in (
            ("catalog_metadata", "singleton,catalog_uuid,schema_version"),
            ("locations", "id,root_path,current_scan_id"),
            ("content", "id,algorithm,digest,size_bytes"),
            ("scan_runs", "id,location_id,started_at_ns,completed_at_ns,status,origin"),
            ("file_observations", "id,scan_id,relative_path,content_id,size_bytes,mtime_ns"),
            ("tags", "id,name"),
            (
                "content_tag_assertions",
                "id,content_id,tag_id,provenance_kind,source_name,source_version,"
                "source_detail,asserted_at_ns,retracted_at_ns",
            ),
        ):
            connection.execute(f"SELECT {fields} FROM {schema}.{table} LIMIT 0").close()
    if version == 4:
        connection.execute(
            f"SELECT operation_id,spec,status,started_at_ns,completed_at_ns,counts,request_digest,"
            f"committed_revision FROM {schema}.operations LIMIT 0"
        ).close()
        definition = connection.execute(
            f"SELECT sql FROM {schema}.sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone()
        expected = " ".join(OPERATION_SCHEMA.split(";")[0].split())
        if definition is None or " ".join(str(definition[0]).split()) != expected:
            raise InvalidCatalogError("catalog operation constraints do not match schema 4")


class Catalog:
    """A persistent, observational catalog of local regular files."""

    def __init__(self, database_path: Path, connection: sqlite3.Connection) -> None:
        self._database_path = database_path.resolve(strict=False)
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.create_collation("archiver_path_components", _compare_relative_path_components)
        self._connection.create_collation(
            "archiver_path_key",
            lambda a, b: (os.path.normcase(a) > os.path.normcase(b)) - (os.path.normcase(a) < os.path.normcase(b)),
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        # Allocate connection-local staging before any query cursors can be open.
        # Publication only changes rows: DDL would lock active pinned readers.
        self._connection.execute(
            "CREATE TEMP TABLE inventory_affected(path TEXT COLLATE archiver_path_key PRIMARY KEY)"
        ).close()

    @classmethod
    def create(cls, database_path: Path) -> Catalog:
        """Create a new schema-version-4 catalog at an unused path."""
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
        """Explicitly add operation records to schema 3; reject older schemas."""
        path = database_path.resolve(strict=False)
        if not path.is_file():
            raise InvalidCatalogError(f"catalog database does not exist: {path}")
        connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            version_row = connection.execute(
                "SELECT catalog_uuid, schema_version FROM catalog_metadata WHERE singleton=1"
            ).fetchone()
            if version_row is None:
                raise InvalidCatalogError("catalog metadata is missing")
            if version_row[1] != 3:
                connection.close()
                with cls.open(path):
                    return False
            from uuid import UUID

            UUID(version_row[0])
            _validate_schema_tables(connection, 3)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise InvalidCatalogError("catalog has broken foreign keys")
            connection.execute("BEGIN IMMEDIATE")
            locked_version = connection.execute(
                "SELECT schema_version FROM catalog_metadata WHERE singleton=1"
            ).fetchone()
            if locked_version is None or locked_version[0] != 3:
                raise InvalidCatalogError("catalog version changed during migration; reopen and retry")
            # executescript would commit before DDL; execute statements individually.
            for statement in OPERATION_SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("UPDATE catalog_metadata SET schema_version=4 WHERE singleton=1 AND schema_version=3")
            connection.commit()
            return True
        except (sqlite3.Error, ValueError) as error:
            connection.rollback()
            raise InvalidCatalogError("could not migrate catalog from schema 3") from error
        finally:
            connection.close()

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
            if version in (1, 2):
                raise InvalidCatalogError(
                    f"catalog schema version {version} requires an explicit rebuild in a new database; "
                    "rescanning does not preserve tags or provenance"
                )
            if version == 3:
                raise InvalidCatalogError("catalog schema version 3 requires explicit Catalog.migrate() to schema 4")
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

    def operation(self, operation_id: str) -> OperationRecord | None:
        """Read an operation summary or receipt by exact ID."""
        row = self._connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        return OperationRecord(
            OperationSpec._decode(str(row["spec"])),
            str(row["status"]),
            int(row["started_at_ns"]),
            row["completed_at_ns"],
            OperationCounts(**json.loads(row["counts"])),
            row["request_digest"],
            None if row["committed_revision"] is None else decode_revision(json.loads(row["committed_revision"])),
        )

    def operation_scope(self, root: Path) -> OperationScope:
        """Capture a read-only operation endpoint and its opaque current revision."""
        canonical_root = safe_root(root)
        if self._connection.in_transaction:
            raise OperationError("cannot capture an operation scope inside a caller transaction")
        try:
            self._connection.execute("BEGIN")
            location = self._location_for_canonical_root(canonical_root)
            snapshot = None
            if location is not None:
                assert location.id is not None
                snapshot = self._operation_snapshot(location)
            scope = OperationScope(self.database_path, InventoryRevision(self.catalog_uuid, canonical_root, snapshot))
            self._connection.rollback()
            return scope
        except BaseException:
            self._connection.rollback()
            raise

    def operations(self, *, limit: int = 100, offset: int = 0) -> tuple[OperationRecord, ...]:
        """Read a bounded page ordered by start time and operation UUID."""
        positive(limit)
        if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
            raise ValueError("offset must be a nonnegative SQLite integer")
        rows = self._connection.execute(
            "SELECT operation_id FROM operations ORDER BY started_at_ns, operation_id LIMIT ? OFFSET ?", (limit, offset)
        )
        result = []
        for row in rows:
            record = self.operation(str(row[0]))
            assert record is not None
            result.append(record)
        return tuple(result)

    def _validate_operation_target(self, spec: OperationSpec) -> None:
        if spec.target.catalog_uuid != self.catalog_uuid or not os.path.samefile(
            spec.target.database_path, self.database_path
        ):
            raise OperationError("operation belongs to another Catalog")

    def _start_operation(self, spec: OperationSpec) -> OperationRecord:
        self._validate_operation_target(spec)
        if self._connection.in_transaction:
            raise OperationError("cannot start an operation inside a caller transaction")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            prior = self.operation(spec.operation_id)
            if prior is not None:
                if prior.spec != spec or prior.status != "executing":
                    raise OperationError("operation ID is already used")
            else:
                self._connection.execute(
                    "INSERT INTO operations VALUES (?,?,'executing',?,NULL,?,NULL,NULL)",
                    (spec.operation_id, spec._json(), time_ns(), canonical(asdict(OperationCounts()))),
                )
            self._check_operation_revision(spec)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        record = self.operation(spec.operation_id)
        assert record is not None
        return record

    def _operation_snapshot(self, location: Location) -> int | None:
        assert location.id is not None
        snapshot = self._current_scan_id(location.id)
        if snapshot is not None:
            row = self._connection.execute(
                "SELECT location_id,status,completed_at_ns,origin FROM scan_runs WHERE id=?", (snapshot,)
            ).fetchone()
            if (
                row is None
                or row["location_id"] != location.id
                or row["status"] != "completed"
                or row["completed_at_ns"] is None
                or row["origin"] not in ("full_scan", "targeted_update")
            ):
                raise OperationError("invalid operation inventory pointer")
        return snapshot

    def _check_operation_revision(self, spec: OperationSpec) -> None:
        location = self._location_for_canonical_root(spec.target.root)
        current = None
        if location is not None:
            assert location.id is not None
            current = self._operation_snapshot(location)
        if current != spec.target.revision._snapshot_id:
            raise StaleRefreshError("operation target baseline is stale")

    def _fence_operation(self, spec: OperationSpec, *, status: str) -> OperationRecord:
        """Resolve receipt versus abandonment under the same lock as finalization."""
        if status not in ("discarded", "superseded"):
            raise ValueError("invalid fence status")
        self._validate_operation_target(spec)
        if self._connection.in_transaction:
            raise OperationError("cannot fence inside a caller transaction")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            prior = self.operation(spec.operation_id)
            if prior is None or prior.spec != spec:
                raise OperationError("missing or mismatched operation")
            if prior.request_digest is None and prior.status == "executing":
                self._connection.execute(
                    "UPDATE operations SET status=?,completed_at_ns=? WHERE operation_id=?",
                    (status, time_ns(), spec.operation_id),
                )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        record = self.operation(spec.operation_id)
        assert record is not None
        return record

    def _finalize_operation(self, request: FinalizationRequest, changes: InventoryChanges) -> OperationRecord:
        """Database-only receipt boundary; no filesystem verification or execution."""
        if prepare_operation_request(request.spec, request.counts, changes) != request:
            raise OperationError("finalization request differs from frozen changes")
        self._validate_operation_target(request.spec)
        self._publish_inventory(
            changes.location,
            changes.baseline._snapshot_id,
            changes._upserts,
            changes._removals,
            origin="targeted_update",
            keep_last=request.spec.keep_last,
            operation_request=request,
        )
        record = self.operation(request.spec.operation_id)
        assert record is not None
        return record

    def _write_operation_receipt(self, request: FinalizationRequest, result: InventoryUpdateResult) -> None:
        self._connection.execute(
            """UPDATE operations SET status=?, completed_at_ns=?, counts=?, request_digest=?,
               committed_revision=? WHERE operation_id=?""",
            (
                request.counts.status,
                time_ns(),
                canonical(asdict(request.counts)),
                request.digest,
                canonical(revision_data(result.revision)),
                request.spec.operation_id,
            ),
        )

    def compare_content(self, target: Catalog, *, source_root: Path, target_root: Path) -> ContentComparison:
        """Create a read-only scoped comparison; enter it before reading results."""
        if not isinstance(target, Catalog):
            raise TypeError("target must be a Catalog")
        return ContentComparison(self, target, source_root=source_root, target_root=target_root)

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def scan_directory(
        self,
        root: Path,
        *,
        verify_hashes: bool = False,
        keep_last: int | None = None,
        excluded_directories: Collection[Path] = (),
        progress_callback: Callable[[ScanProgress], None] | None = None,
    ) -> ScanSummary:
        """Reconcile and apply observations, optionally hashing every included file."""
        self._validate_retention(keep_last)
        change_set = self.reconcile_directory(
            root,
            verify_hashes=verify_hashes,
            excluded_directories=excluded_directories,
            progress_callback=progress_callback,
        )
        return self.apply_refresh(change_set, keep_last=keep_last)

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

    def apply_refresh(self, change_set: RefreshChangeSet, *, keep_last: int | None = None) -> ScanSummary:
        """Publish a full inventory atomically after checking its baseline."""
        self._validate_retention(keep_last)
        try:
            result = self._publish_inventory(
                change_set.location,
                change_set.baseline_scan_id,
                tuple(change.current for change in change_set.changes if change.current is not None),
                (),
                origin="full_scan",
                keep_last=keep_last,
            )
        except StaleRefreshError:
            raise
        except (ValueError, RefreshFailure) as error:
            raise RefreshFailure(f"could not apply refresh: {error}") from error
        assert result.inventory is not None
        return result.inventory

    @staticmethod
    def _validate_retention(keep_last: int | None) -> None:
        if keep_last is not None:
            if type(keep_last) is not int:
                raise TypeError("keep_last must be an integer or None")
            if not 1 <= keep_last <= 2**63 - 1:
                raise ValueError("keep_last must be between 1 and 2**63 - 1")

    def _baseline_file(self, snapshot: int | None, path: PurePosixPath) -> FileObservation | None:
        statement = """SELECT o.relative_path, o.size_bytes, o.mtime_ns, c.algorithm, c.digest,
                      l.id, l.root_path
               FROM file_observations o JOIN content c ON c.id=o.content_id
               JOIN scan_runs s ON s.id=o.scan_id JOIN locations l ON l.id=s.location_id
               WHERE o.scan_id=? AND o.relative_path = ?"""
        with closing(self._connection.execute(statement, (snapshot, path.as_posix()))) as cursor:
            row = cursor.fetchone()
        if row is None and os.name == "nt":
            with closing(
                self._connection.execute(statement + " COLLATE archiver_path_key", (snapshot, path.as_posix()))
            ) as cursor:
                row = cursor.fetchone()
        if row is None:
            return None
        return FileObservation(
            Location(int(row["id"]), Path(row["root_path"])),
            PurePosixPath(row["relative_path"]),
            ContentId(row["algorithm"], row["digest"]),
            int(row["size_bytes"]),
            int(row["mtime_ns"]),
        )

    def prepare_file_update(
        self,
        root: Path,
        *,
        paths: Path | Collection[Path] = (),
        remove_paths: Collection[PurePosixPath] = (),
        verify_hashes: bool = False,
    ) -> InventoryChanges:
        """Observe selected files against one baseline; never enumerate the root or write."""
        if type(verify_hashes) is not bool:
            raise TypeError("verify_hashes must be bool")
        if self._connection.in_transaction:
            raise RefreshFailure("cannot prepare inventory inside an existing transaction")
        # Inspect the original root before resolving away any symlink ancestors.
        for ancestor in (root, *root.parents):
            if ancestor.is_symlink() or ancestor.is_junction():
                raise ValueError("root must not traverse symbolic links or junctions")
        canonical_root = self._canonical_root(root)
        selected = (paths,) if isinstance(paths, Path) else tuple(paths)
        validated: list[tuple[Path, PurePosixPath, bool]] = []
        for supplied in selected:
            absolute, relative = targeted_path(canonical_root, supplied)
            if self._is_catalog_file(absolute) or not absolute.is_file():
                raise ValueError("selected path must be a regular non-catalog file")
            validated.append((absolute, relative, False))
        for relative in remove_paths:
            if not isinstance(relative, PurePosixPath):
                raise TypeError("removal paths must be PurePosixPath values")
            self._validated_relative_path(relative)
            absolute, normalized = targeted_path(canonical_root, Path(relative.as_posix()))
            if self._is_catalog_file(absolute):
                raise ValueError("cannot remove catalog database artifacts")
            validated.append((absolute, normalized, True))
        seen: set[str] = set()
        for _, relative, _ in validated:
            key = os.path.normcase(relative.as_posix())
            if key in seen:
                raise ValueError("duplicate or conflicting inventory paths")
            seen.add(key)
        validated.sort(key=lambda item: item[1].parts)
        # Fetch only selected baseline records in a single consistent read scope.
        # Release the read transaction before hashing, allowing other writers.
        self._connection.execute("BEGIN").close()
        try:
            location = self._location_for_canonical_root(canonical_root) or Location(None, canonical_root)
            snapshot = None if location.id is None else self._current_scan_id(location.id)
            revision = InventoryRevision(self.catalog_uuid, canonical_root, snapshot)
            previous = [self._baseline_file(snapshot, relative) for _, relative, _ in validated]
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        upserts: list[FileObservation] = []
        removals: list[PurePosixPath] = []
        for (absolute, relative, removing), prior in zip(validated, previous, strict=True):
            if prior is not None:
                relative = prior.relative_path
            if removing:
                if prior is None:
                    raise ValueError(f"removal is not in the baseline: {relative}")
                removals.append(relative)
            elif prior is None:
                upserts.append(self._hashed_observation(absolute, location, relative))
            else:
                current, _ = self._reconcile_existing_path(absolute, location, prior, verify_hashes=verify_hashes)
                upserts.append(current)
        return InventoryChanges(location, revision, tuple(upserts), tuple(removals))

    def apply_inventory_changes(
        self,
        changes: InventoryChanges,
        *,
        keep_last: int | None = 2,
    ) -> InventoryUpdateResult:
        """Publish prepared targeted changes; keep two successes unless overridden."""
        if not isinstance(changes, InventoryChanges):
            raise TypeError("changes must be prepared InventoryChanges")
        revision = changes.baseline
        if revision._catalog_uuid != self.catalog_uuid or revision._root != changes.location.root:
            raise StaleRefreshError("inventory changes belong to a different catalog or location")
        return self._publish_inventory(
            changes.location,
            revision._snapshot_id,
            changes._upserts,
            changes._removals,
            origin="targeted_update",
            keep_last=keep_last,
        )

    def _publish_inventory(
        self,
        location: Location,
        baseline: int | None,
        upserts: tuple[FileObservation, ...],
        removals: tuple[PurePosixPath, ...],
        *,
        origin: str,
        keep_last: int | None,
        operation_request: FinalizationRequest | None = None,
    ) -> InventoryUpdateResult:
        self._validate_retention(keep_last)
        if self._connection.in_transaction:
            raise RefreshFailure("cannot publish inventory inside an existing transaction")
        paths: set[str] = set()
        for relative in (*[item.relative_path for item in upserts], *removals):
            self._validated_relative_path(relative)
            key = os.path.normcase(relative.as_posix())
            if key in paths:
                raise ValueError("duplicate or conflicting inventory paths")
            paths.add(key)
        for item in upserts:
            if item.location != location or type(item.size_bytes) is not int or item.size_bytes < 0:
                raise ValueError("invalid inventory observation location or size")
            if type(item.mtime_ns) is not int or not isinstance(item.content_id, ContentId):
                raise ValueError("invalid inventory observation metadata")
        began = False
        try:
            self._connection.execute("BEGIN IMMEDIATE").close()
            began = True
            if operation_request is not None:
                operation_prior = self.operation(operation_request.spec.operation_id)
                if operation_prior is None or operation_prior.spec != operation_request.spec:
                    raise OperationError("operation was not started with this scope")
                if operation_prior.request_digest is not None:
                    if operation_prior.request_digest != operation_request.digest:
                        raise OperationError("operation already committed a different request")
                    assert operation_prior.committed_revision is not None
                    self._connection.commit()
                    began = False
                    return InventoryUpdateResult(operation_prior.committed_revision, None, 0, 0, 0, None, None)
                if operation_prior.status != "executing":
                    raise OperationError("operation is fenced against publication")
            existing = self._location_for_canonical_root(location.root)
            current = None
            if existing is not None:
                assert existing.id is not None
                current = self._current_scan_id(existing.id)
            if current != baseline or (location.id is not None and (existing is None or existing.id != location.id)):
                raise StaleRefreshError("inventory baseline no longer matches catalog current state")
            if origin == "targeted_update" and not upserts and not removals:
                scan = None if existing is None else execute_current_scan(self._connection, existing)
                result = InventoryUpdateResult(
                    InventoryRevision(self.catalog_uuid, location.root, current),
                    None if scan is None else scan.origin,
                    0,
                    0,
                    0,
                    None if current is None else self._scan_summary(current),
                    None,
                )
                if operation_request is not None:
                    self._write_operation_receipt(operation_request, result)
                self._connection.commit()
                return result
            if existing is None:
                cursor = self._connection.execute("INSERT INTO locations(root_path) VALUES (?)", (str(location.root),))
                assert cursor.lastrowid is not None
                existing = Location(int(cursor.lastrowid), location.root)
            assert existing.id is not None
            replaced = 0
            for item in upserts if origin == "targeted_update" else ():
                prior = self._baseline_file(baseline, item.relative_path)
                if prior is not None:
                    if prior.relative_path != item.relative_path:
                        raise ValueError("upsert aliases an existing baseline path")
                    replaced += 1
            for relative in removals:
                prior = self._baseline_file(baseline, relative)
                if prior is None or prior.relative_path != relative:
                    raise ValueError("removal must identify a baseline path exactly")
            snapshot = self._insert_running_scan(existing.id, origin)
            self._connection.execute("DELETE FROM inventory_affected").close()
            self._connection.executemany(
                "INSERT INTO inventory_affected VALUES (?)", ((path,) for path in paths)
            ).close()
            if origin == "targeted_update":
                self._connection.execute(
                    """INSERT INTO file_observations(scan_id, relative_path, content_id, size_bytes, mtime_ns)
                       SELECT ?, relative_path, content_id, size_bytes, mtime_ns FROM file_observations o
                       WHERE scan_id=? AND NOT EXISTS
                       (SELECT 1 FROM inventory_affected a WHERE a.path=o.relative_path COLLATE archiver_path_key)""",
                    (snapshot, baseline),
                ).close()
            for item in upserts:
                content = self._get_or_create_content(item.content_id, item.size_bytes)
                self._connection.execute(
                    """INSERT INTO file_observations(scan_id, relative_path, content_id, size_bytes, mtime_ns)
                       VALUES (?, ?, ?, ?, ?)""",
                    (snapshot, item.relative_path.as_posix(), content, item.size_bytes, item.mtime_ns),
                ).close()
            self._connection.execute("DELETE FROM inventory_affected").close()
            self._complete_scan(existing.id, snapshot)
            retention = None
            if keep_last is not None:
                parameters = {"location_id": existing.id, "keep_last": keep_last, "include_failed": 0}
                retention = self._history_prune_summary(location.root, parameters, dry_run=False)
                self._delete_history(parameters, retention)
            result = InventoryUpdateResult(
                InventoryRevision(self.catalog_uuid, location.root, snapshot),
                origin,
                len(upserts) - replaced,
                replaced,
                len(removals),
                self._scan_summary(snapshot),
                retention,
            )
            if operation_request is not None:
                self._write_operation_receipt(operation_request, result)
            self._connection.commit()
            began = False
            return result
        except BaseException as error:
            if began:
                self._connection.rollback()
            if isinstance(error, sqlite3.Error):
                raise RefreshFailure("could not publish inventory") from error
            raise

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
        with closing(
            self._connection.execute(
                "SELECT 1 FROM scan_runs WHERE id = ? AND location_id = ? AND status = 'completed'",
                (handle._scan_id, handle._location.id),
            )
        ) as cursor:
            if cursor.fetchone() is None:
                raise PrunedScanError(f"scan {handle._scan_id} is no longer available for duplicate member reads")
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

    def scan_summaries(self, root: Path) -> Iterator[ScanHistorySummary]:
        """Yield newest-first persisted scan totals; incomplete scans are partial.

        Reads begin on first advancement. Close the iterator before pruning;
        the originating catalog must remain open while consuming it.
        """
        location = self._location_for_root(root)
        if location is not None:
            yield from iter_scan_summaries(self._connection, location)

    def prune_history(
        self,
        root: Path,
        *,
        keep_last: int = 3,
        dry_run: bool = True,
        include_failed: bool = False,
    ) -> PruneHistorySummary:
        """Preview or atomically remove old snapshots without changing inventory.

        Keep the newest successful scans by ID. Opt-in failed cleanup removes
        only failures before the oldest retained successful scan. Never delete
        running/current scans, content, or tags. Close readers before applying;
        handles for deleted scans can no longer load members. No VACUUM is run.
        """
        if type(keep_last) is not int:
            raise TypeError("keep_last must be an integer")
        if not 1 <= keep_last <= 2**63 - 1:
            raise ValueError("keep_last must be between 1 and 2**63 - 1")
        if type(dry_run) is not bool or type(include_failed) is not bool:
            raise TypeError("dry_run and include_failed must be bools")
        if self._connection.in_transaction:
            raise HistoryPruningError("cannot prune history inside an existing transaction")
        canonical_root = self._canonical_root(root)
        began = False
        try:
            self._connection.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE").close()
            began = True
            location = self._location_for_canonical_root(canonical_root)
            if location is None:
                summary = PruneHistorySummary(canonical_root, keep_last, dry_run, include_failed, 0, None, 0, 0, 0, 0)
            else:
                assert location.id is not None
                parameters = {"location_id": location.id, "keep_last": keep_last, "include_failed": int(include_failed)}
                summary = self._history_prune_summary(canonical_root, parameters, dry_run=dry_run)
                self._delete_history(parameters, summary, dry_run=dry_run)
            self._connection.commit()
            began = False
            return summary
        except BaseException as error:
            if began:
                self._connection.rollback()
            if isinstance(error, sqlite3.Error):
                raise HistoryPruningError(f"could not prune history for {canonical_root}") from error
            raise

    def _delete_history(
        self,
        parameters: dict[str, int],
        summary: PruneHistorySummary,
        *,
        dry_run: bool = False,
    ) -> None:
        with closing(
            self._connection.execute(
                _HISTORY_RETENTION_CTE
                + """
            SELECT 1 FROM locations
            WHERE current_scan_id IN (SELECT id FROM candidates) LIMIT 1
            """,
                parameters,
            )
        ) as cursor:
            if cursor.fetchone() is not None:
                raise HistoryPruningError("pruning would remove a referenced current scan")
        if dry_run:
            return
        for statement, expected in (
            (
                "DELETE FROM file_observations WHERE scan_id IN (SELECT id FROM candidates)",
                summary.observations_to_remove,
            ),
            ("DELETE FROM scan_runs WHERE id IN (SELECT id FROM candidates)", summary.scans_to_remove),
        ):
            self._connection.execute(_HISTORY_RETENTION_CTE + statement, parameters).close()
            with closing(self._connection.execute("SELECT changes()")) as cursor:
                row = cursor.fetchone()
                assert row is not None
                if int(row[0]) != expected:
                    raise HistoryPruningError("history deletion count did not match its transaction report")

    def _history_prune_summary(self, root: Path, parameters: dict[str, int], *, dry_run: bool) -> PruneHistorySummary:
        with closing(
            self._connection.execute(
                _HISTORY_RETENTION_CTE
                + """
            SELECT (SELECT COUNT(*) FROM retained) AS retained_count,
                   (SELECT MIN(id) FROM retained) AS cutoff,
                   COUNT(*) AS scan_count,
                   COALESCE(SUM(status = 'completed'), 0) AS successful_count,
                   COALESCE(SUM(status = 'failed'), 0) AS failed_count,
                   (SELECT COUNT(*) FROM file_observations
                    WHERE scan_id IN (SELECT id FROM candidates)) AS observation_count
            FROM candidates
            """,
                parameters,
            )
        ) as cursor:
            row = cursor.fetchone()
            assert row is not None
            return PruneHistorySummary(
                root=root,
                keep_last=parameters["keep_last"],
                dry_run=dry_run,
                include_failed=bool(parameters["include_failed"]),
                successful_scans_retained=int(row["retained_count"]),
                oldest_retained_scan_id=None if row["cutoff"] is None else int(row["cutoff"]),
                scans_to_remove=int(row["scan_count"]),
                observations_to_remove=int(row["observation_count"]),
                successful_scans_to_remove=int(row["successful_count"]),
                failed_scans_to_remove=int(row["failed_count"]),
            )

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

    def _current_scan_id(self, location_id: int) -> int | None:
        row = self._connection.execute("SELECT current_scan_id FROM locations WHERE id = ?", (location_id,)).fetchone()
        if row is None:
            raise StaleRefreshError("refresh location no longer exists in the catalog")
        value = row["current_scan_id"]
        return None if value is None else int(value)

    def _insert_running_scan(self, location_id: int, origin: str = "full_scan") -> int:
        cursor = self._connection.execute(
            "INSERT INTO scan_runs (location_id, started_at_ns, status, origin) VALUES (?, ?, 'running', ?)",
            (location_id, time_ns(), origin),
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
