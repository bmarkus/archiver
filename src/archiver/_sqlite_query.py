"""SQLite compilation and execution for internal query descriptions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from ._query import CurrentObservationQuery
from .models import ContentId, FileObservation
from .query import (
    _CurrentFileField,
    _SortDirection,
    _validate_path_glob_predicate,
    _validate_sort_expression,
)

_ORDER_COLUMNS = {
    _CurrentFileField.PATH: "observations.relative_path",
    _CurrentFileField.SIZE: "observations.size_bytes",
    _CurrentFileField.MTIME: "observations.mtime_ns",
}

_ORDER_DIRECTIONS = {
    _SortDirection.ASCENDING: "ASC",
    _SortDirection.DESCENDING: "DESC",
}


@dataclass(frozen=True, slots=True)
class SqliteQuery:
    """One parameterized SQLite statement ready for execution."""

    statement: str
    parameters: tuple[object, ...]


def compile_current_observations(query: CurrentObservationQuery) -> SqliteQuery:
    """Compile one current-observation description into parameterized SQLite."""
    where_clauses = ["locations.id = ?", "scans.id = locations.current_scan_id"]
    parameters: list[object] = [query.location.id]
    for predicate in query.predicates:
        _validate_path_glob_predicate(predicate)
        where_clauses.append("observations.relative_path GLOB ?")
        parameters.append(predicate.pattern)

    _validate_sort_expression(query.ordering)
    order_column = _ORDER_COLUMNS[query.ordering.field]
    order_direction = _ORDER_DIRECTIONS[query.ordering.direction]
    tie_breaker = "" if query.ordering.field is _CurrentFileField.PATH else ", observations.relative_path ASC"
    limit_clause = ""
    if query.limit is not None:
        limit_clause = "LIMIT ?"
        parameters.append(query.limit)

    where_clause = " AND\n              ".join(where_clauses)
    return SqliteQuery(
        statement=f"""
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
            WHERE {where_clause}
            ORDER BY {order_column} {order_direction}{tie_breaker}
            {limit_clause}
        """,
        parameters=tuple(parameters),
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
