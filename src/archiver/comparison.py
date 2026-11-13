"""Read-only, snapshot-scoped cross-catalog content comparison."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID

from .errors import ContentComparisonError, InvalidCatalogError, ScanFailure
from .models import ComparedContent, ComparisonScope, ContentComparisonSummary, ContentId, InventoryRevision, Location

if TYPE_CHECKING:
    from .catalog import Catalog


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", uri=True)


def _relations(target_alias: str) -> str:
    # Aliases are selected internally, never supplied by a caller.
    return f"""
    WITH source_content AS (
        SELECT c.algorithm, c.digest, c.size_bytes,
               MIN(o.relative_path COLLATE BINARY) AS relative_path, COUNT(*) AS instance_count
        FROM source_catalog.file_observations o
        JOIN source_catalog.content c ON c.id = o.content_id
        WHERE o.scan_id = :source_scan
        GROUP BY c.algorithm, c.digest
    ), target_content AS (
        SELECT c.algorithm, c.digest, c.size_bytes
        FROM {target_alias}.file_observations o
        JOIN {target_alias}.content c ON c.id = o.content_id
        WHERE o.scan_id = :target_scan
        GROUP BY c.algorithm, c.digest
    ), classified AS (
        SELECT s.*, t.digest IS NOT NULL AS in_target
        FROM source_content s LEFT JOIN target_content t
          ON t.algorithm = s.algorithm AND t.digest = s.digest
    )
    """


class ContentComparison:
    """Single-use comparison context. Close readers before publishing inventories.

    Each input is captured independently, rather than at a globally atomic instant.
    Python memory depends on batch size; SQLite may need larger grouping/sort space.
    """

    def __init__(self, source: Catalog, target: Catalog, *, source_root: Path, target_root: Path) -> None:
        for root in (source_root, target_root):
            if not isinstance(root, Path):
                raise TypeError("comparison roots must be Path values")
        try:
            self._source_root = source._canonical_root(source_root)
            self._target_root = target._canonical_root(target_root)
        except (OSError, ScanFailure) as error:
            raise ContentComparisonError("comparison roots must be accessible directories") from error
        self._source = source
        self._target = target
        self._connection: sqlite3.Connection | None = None
        self._cursor: sqlite3.Cursor | None = None
        self._iterator: Generator[tuple[ComparedContent, ...], None, None] | None = None
        self._entered = False
        self._closed = False
        self._source_scope: ComparisonScope | None = None
        self._target_scope: ComparisonScope | None = None
        self._summary: ContentComparisonSummary | None = None
        self._parameters: dict[str, int | None] = {}
        self._relations = ""

    def __enter__(self) -> ContentComparison:
        if self._entered or self._closed:
            raise ContentComparisonError("comparison sessions can be entered only once")
        self._entered = True
        try:
            # Probe both caller connections without altering their transaction state.
            uuids = []
            for catalog in (self._source, self._target):
                if catalog._connection.in_transaction:
                    raise ContentComparisonError("close caller-owned transactions before comparison")
                with closing(
                    catalog._connection.execute("SELECT catalog_uuid FROM catalog_metadata WHERE singleton = 1")
                ) as cursor:
                    metadata = cursor.fetchone()
                if metadata is None:
                    raise ContentComparisonError("catalog metadata is missing")
                uuids.append(metadata[0])
            paths = (self._source.database_path, self._target.database_path)
            identities = tuple(path.stat() for path in paths)
            same_file = os.path.samestat(*identities)
            connection = _connect()
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON").close()
            connection.execute("ATTACH DATABASE ? AS source_catalog", (paths[0].as_uri() + "?mode=ro",)).close()
            target_alias = "source_catalog" if same_file else "target_catalog"
            if not same_file:
                connection.execute("ATTACH DATABASE ? AS target_catalog", (paths[1].as_uri() + "?mode=ro",)).close()
            for path, identity in zip(paths, identities, strict=True):
                if not os.path.samestat(identity, path.stat()):
                    raise ContentComparisonError("catalog database changed during comparison opening")
            connection.execute("PRAGMA query_only = ON").close()
            connection.execute("BEGIN").close()
            self._source_scope = self._capture("source_catalog", uuids[0], self._source_root)
            self._target_scope = self._capture(target_alias, uuids[1], self._target_root)
            self._parameters = {
                "source_scan": self._source_scope.revision._snapshot_id,
                "target_scan": self._target_scope.revision._snapshot_id,
            }
            self._relations = _relations(target_alias)
            with closing(
                connection.execute(
                    self._relations
                    + """
                SELECT 1 FROM source_content s JOIN target_content t
                  ON s.algorithm = t.algorithm AND s.digest = t.digest
                WHERE s.size_bytes != t.size_bytes LIMIT 1
            """,
                    self._parameters,
                )
            ) as cursor:
                if cursor.fetchone() is not None:
                    raise ContentComparisonError("contradictory sizes for shared content")
            self._summary = self._read_summary()
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, (sqlite3.Error, OSError, ValueError, InvalidCatalogError)):
                raise ContentComparisonError(f"could not open content comparison: {error}") from error
            raise

    def _capture(self, alias: str, expected_uuid: str, root: Path) -> ComparisonScope:
        from .catalog import SCHEMA_VERSION, _validate_schema_tables

        connection = self._require_connection()
        with closing(
            connection.execute(f"SELECT catalog_uuid, schema_version FROM {alias}.catalog_metadata")
        ) as cursor:
            row = cursor.fetchone()
            if row is None or cursor.fetchone() is not None:
                raise ContentComparisonError("catalog metadata must contain one row")
        if row["schema_version"] in (1, 2):
            raise ContentComparisonError(
                "legacy catalog requires an explicit rebuild; rescanning does not preserve tags or provenance"
            )
        if row["schema_version"] != SCHEMA_VERSION:
            raise ContentComparisonError(f"unsupported catalog schema version: {row['schema_version']}")
        catalog_uuid = row["catalog_uuid"]
        if not isinstance(catalog_uuid, str):
            raise ContentComparisonError("invalid catalog UUID")
        UUID(catalog_uuid)
        if catalog_uuid != expected_uuid:
            raise ContentComparisonError("catalog database replaced with a different catalog")
        _validate_schema_tables(connection, SCHEMA_VERSION, schema=alias)
        # Check required fields even when the selected inventory is absent/empty.
        for table, fields in (
            ("locations", "id, root_path, current_scan_id"),
            ("scan_runs", "id, location_id, status, origin, completed_at_ns"),
            ("content", "id, algorithm, digest, size_bytes"),
            ("file_observations", "scan_id, relative_path, content_id, size_bytes, mtime_ns"),
        ):
            connection.execute(f"SELECT {fields} FROM {alias}.{table} LIMIT 0").close()
        with closing(
            connection.execute(f"SELECT id, current_scan_id FROM {alias}.locations WHERE root_path = ?", (str(root),))
        ) as cursor:
            row = cursor.fetchone()
        location = Location(None if row is None else int(row["id"]), root)
        scan_id = None if row is None else row["current_scan_id"]
        if scan_id is not None:
            with closing(
                connection.execute(
                    f"SELECT location_id, status, completed_at_ns, origin FROM {alias}.scan_runs WHERE id = ?",
                    (scan_id,),
                )
            ) as cursor:
                snapshot = cursor.fetchone()
            if (
                snapshot is None
                or snapshot["location_id"] != location.id
                or snapshot["status"] != "completed"
                or snapshot["completed_at_ns"] is None
                or snapshot["origin"] not in ("full_scan", "targeted_update")
            ):
                raise ContentComparisonError("invalid current inventory pointer")
            self._validate_observations(alias, scan_id)
        return ComparisonScope(
            catalog_uuid, root, location, InventoryRevision(catalog_uuid, root, scan_id), scan_id is not None
        )

    def _validate_observations(self, alias: str, scan_id: int) -> None:
        # SQL checks every scoped row, including identities omitted from batches.
        sql = f"""
            SELECT 1 FROM {alias}.file_observations o
            LEFT JOIN {alias}.content c ON c.id = o.content_id
            WHERE o.scan_id = ? AND (
                c.id IS NULL OR typeof(c.algorithm) != 'text' OR c.algorithm != 'sha256' OR typeof(c.digest) != 'text'
                OR length(c.digest) != 64 OR c.digest GLOB '*[^0-9a-f]*'
                OR typeof(c.size_bytes) != 'integer' OR c.size_bytes < 0
                OR typeof(o.size_bytes) != 'integer' OR o.size_bytes != c.size_bytes
                OR typeof(o.mtime_ns) != 'integer' OR typeof(o.relative_path) != 'text'
                OR o.relative_path IN ('', '.') OR o.relative_path LIKE '/%'
                OR instr(o.relative_path, char(92)) > 0
                OR instr('/' || o.relative_path || '/', '/../') > 0
            ) LIMIT 1
        """
        with closing(self._require_connection().execute(sql, (scan_id,))) as cursor:
            if cursor.fetchone() is not None:
                raise ContentComparisonError("invalid scoped observation, identity, or content size")

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None or self._closed:
            raise ContentComparisonError("comparison must be entered and still open")
        return self._connection

    @property
    def source_scope(self) -> ComparisonScope:
        """Return immutable captured source metadata (also available after close)."""
        if self._source_scope is None:
            raise ContentComparisonError("source scope has not been captured")
        return self._source_scope

    @property
    def target_scope(self) -> ComparisonScope:
        """Return immutable captured target metadata (also available after close)."""
        if self._target_scope is None:
            raise ContentComparisonError("target scope has not been captured")
        return self._target_scope

    def _read_summary(self) -> ContentComparisonSummary:
        with closing(
            self._require_connection().execute(
                self._relations
                + """
            SELECT COUNT(*) AS source_count,
                   (SELECT COUNT(*) FROM target_content) AS target_count,
                   COALESCE(SUM(in_target), 0) AS shared_count,
                   COALESCE(SUM(CASE WHEN in_target = 0 THEN size_bytes ELSE 0 END), 0) AS bytes_to_copy
            FROM classified
        """,
                self._parameters,
            )
        ) as cursor:
            row = cursor.fetchone()
        assert row is not None
        source, target, shared, bytes_to_copy = (int(value) for value in row)
        return ContentComparisonSummary(source, target, shared, source - shared, target - shared, bytes_to_copy)

    def summary(self) -> ContentComparisonSummary:
        """Return complete counts for the captured inventories."""
        self._require_connection()
        assert self._summary is not None
        return self._summary

    def batches(
        self, *, batch_size: int = 1000, include_present: bool = False
    ) -> Generator[tuple[ComparedContent, ...], None, None]:
        """Read bounded tuples; close an iterator explicitly after an early break."""
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise TypeError("batch_size must be an integer")
        if not 1 <= batch_size <= 2**63 - 1:
            raise ValueError("batch_size must be in 1..2**63-1")
        if not isinstance(include_present, bool):
            raise TypeError("include_present must be a bool")
        connection = self._require_connection()
        if self._iterator is not None:
            raise ContentComparisonError("only one batch iterator may be active")
        try:
            self._cursor = connection.execute(
                self._relations
                + """
                SELECT s.algorithm, s.digest, s.size_bytes, s.relative_path,
                       o.mtime_ns, s.instance_count, s.in_target
                FROM classified s JOIN source_catalog.file_observations o
                  ON o.scan_id = :source_scan AND o.relative_path = s.relative_path
                WHERE :include_present OR s.in_target = 0
                ORDER BY s.algorithm COLLATE BINARY, s.digest COLLATE BINARY
            """,
                {**self._parameters, "include_present": include_present},
            )
            # A requested size is an upper bound. Keep individual SQLite fetches
            # modest and within the runtime's C-int limit even for huge bounds.
            iterator = self._stream(min(batch_size, 10_000))
            # Arm finally before returning, so close() works even before first next().
            next(iterator)
            self._iterator = iterator
            return iterator
        except BaseException as error:
            self.close()
            if isinstance(error, sqlite3.Error):
                raise ContentComparisonError("could not start comparison batches") from error
            raise

    def _stream(self, batch_size: int) -> Generator[tuple[ComparedContent, ...], None, None]:
        try:
            yield ()  # Consumed internally by batches(), never exposed to callers.
            assert self._cursor is not None
            while rows := self._cursor.fetchmany(batch_size):
                yield tuple(
                    ComparedContent(
                        ContentId(row[0], row[1]), row[2], PurePosixPath(row[3]), row[4], row[5], bool(row[6])
                    )
                    for row in rows
                )
        except GeneratorExit:
            raise
        except BaseException as error:
            self._iterator = None  # Do not close a generator while it is executing.
            self.close()
            if isinstance(error, (sqlite3.Error, ValueError)):
                raise ContentComparisonError("could not read comparison batches") from error
            raise
        finally:
            self._release_cursor()
            self._iterator = None

    def _release_cursor(self) -> None:
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None

    def close(self) -> None:
        """Idempotently release comparison resources, leaving caller handles open."""
        if self._iterator is not None:
            self._iterator.close()
        self._release_cursor()
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._closed = True

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
