"""SQLite persistence and read-only local directory scanning."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path, PurePosixPath
from time import time_ns
from uuid import uuid4

from .errors import InvalidCatalogError, ScanFailure
from .hashing import hash_file
from .models import ContentId, FileObservation, Location, ScanSummary

SCHEMA_VERSION = 1

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
"""


class Catalog:
    """A persistent, observational catalog of local regular files."""

    def __init__(self, database_path: Path, connection: sqlite3.Connection) -> None:
        self._database_path = database_path.resolve(strict=False)
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def create(cls, database_path: Path) -> Catalog:
        """Create a new schema-version-1 catalog at an unused path."""
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
            if row[1] != SCHEMA_VERSION:
                raise InvalidCatalogError(f"unsupported catalog schema version: {row[1]} (expected {SCHEMA_VERSION})")
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

    def close(self) -> None:
        """Close the SQLite connection."""
        self._connection.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def scan_directory(self, root: Path) -> ScanSummary:
        """Read a local directory and atomically make its scan current on success."""
        canonical_root = self._canonical_root(root)
        location = self._get_or_create_location(canonical_root)
        scan_id = self._start_scan(location.id)

        try:
            with self._connection:
                for path in self._regular_files(canonical_root):
                    if self._is_catalog_file(path):
                        continue
                    stat = path.stat()
                    content_id = hash_file(path)
                    content_row_id = self._get_or_create_content(content_id, stat.st_size)
                    relative_path = PurePosixPath(path.relative_to(canonical_root).as_posix())
                    self._connection.execute(
                        """
                        INSERT INTO file_observations
                            (scan_id, relative_path, content_id, size_bytes, mtime_ns)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            scan_id,
                            relative_path.as_posix(),
                            content_row_id,
                            stat.st_size,
                            stat.st_mtime_ns,
                        ),
                    )
                self._complete_scan(location.id, scan_id)
        except Exception as error:
            self._mark_failed(scan_id)
            if isinstance(error, ScanFailure):
                raise
            raise ScanFailure(f"scan failed for {canonical_root}") from error

        return self._scan_summary(scan_id)

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

    def _get_or_create_location(self, root: Path) -> Location:
        row = self._connection.execute(
            "SELECT id, root_path FROM locations WHERE root_path = ?", (str(root),)
        ).fetchone()
        if row is None:
            with self._connection:
                cursor = self._connection.execute("INSERT INTO locations (root_path) VALUES (?)", (str(root),))
            assert cursor.lastrowid is not None
            return Location(id=int(cursor.lastrowid), root=root)
        return Location(id=int(row["id"]), root=Path(str(row["root_path"])))

    def _start_scan(self, location_id: int) -> int:
        with self._connection:
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
        # Promote the scan only after every observation was written in this successful transaction.
        self._connection.execute(
            "UPDATE locations SET current_scan_id = ? WHERE id = ?",
            (scan_id, location_id),
        )

    def _mark_failed(self, scan_id: int) -> None:
        with suppress(sqlite3.Error):
            with self._connection:
                self._connection.execute(
                    "UPDATE scan_runs SET status = 'failed' WHERE id = ? AND status = 'running'",
                    (scan_id,),
                )

    def _get_or_create_content(self, content_id: ContentId, size_bytes: int) -> int:
        row = self._connection.execute(
            "SELECT id, size_bytes FROM content WHERE algorithm = ? AND digest = ?",
            (content_id.algorithm, content_id.digest),
        ).fetchone()
        if row is not None:
            if int(row["size_bytes"]) != size_bytes:
                raise ScanFailure("one content identity was observed with contradictory sizes")
            return int(row["id"])
        cursor = self._connection.execute(
            "INSERT INTO content (algorithm, digest, size_bytes) VALUES (?, ?, ?)",
            (content_id.algorithm, content_id.digest, size_bytes),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def _regular_files(self, directory: Path) -> Iterator[Path]:
        for entry in sorted(directory.iterdir(), key=lambda candidate: candidate.name):
            if entry.is_symlink():
                continue
            if entry.is_dir():
                yield from self._regular_files(entry)
            elif entry.is_file():
                yield entry

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

        # Start with historical observations, join their scan/location/content context, then apply

    # a fixed internal filter and rebuild the rows as typed FileObservation values.
    def _observations_for_query(self, where_clause: str, parameters: tuple[object, ...]) -> list[FileObservation]:
        rows = self._connection.execute(
            f"""
            SELECT
                locations.id AS location_id,
                locations.root_path AS root_path,
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
        ).fetchall()
        return [
            FileObservation(
                location=Location(id=int(row["location_id"]), root=Path(str(row["root_path"]))),
                relative_path=PurePosixPath(str(row["relative_path"])),
                content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
                size_bytes=int(row["size_bytes"]),
                mtime_ns=int(row["mtime_ns"]),
            )
            for row in rows
        ]
