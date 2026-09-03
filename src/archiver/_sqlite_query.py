"""SQLite compilation and execution for internal query descriptions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from ._query import CurrentObservationQuery
from .models import ContentId, FileObservation


@dataclass(frozen=True, slots=True)
class SqliteQuery:
    """One parameterized SQLite statement ready for execution."""

    statement: str
    parameters: tuple[object, ...]


def compile_current_observations(query: CurrentObservationQuery) -> SqliteQuery:
    """Compile one current-observation description into parameterized SQLite."""
    return SqliteQuery(
        statement="""
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
            WHERE locations.id = ?
              AND scans.id = locations.current_scan_id
            ORDER BY observations.relative_path ASC
        """,
        parameters=(query.location.id,),
    )


def execute_current_observations(
    connection: sqlite3.Connection, query: CurrentObservationQuery
) -> list[FileObservation]:
    """Execute and materialize one current-observation query."""
    compiled = compile_current_observations(query)
    rows = connection.execute(compiled.statement, compiled.parameters)
    return [
        FileObservation(
            location=query.location,
            relative_path=PurePosixPath(str(row["relative_path"])),
            content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
            size_bytes=int(row["size_bytes"]),
            mtime_ns=int(row["mtime_ns"]),
        )
        for row in rows
    ]
