"""SQLite compilation and execution for internal query descriptions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

from ._query import CurrentObservationQuery
from .models import ContentId, FileObservation, Location
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


def _compile_current_observation_scope(query: CurrentObservationQuery) -> tuple[str, tuple[object, ...]]:
    """Compile the shared current-state relation and typed predicates."""
    where_clauses = ["locations.id = ?", "scans.id = locations.current_scan_id"]
    parameters: list[object] = [query.location.id]
    for predicate in query.predicates:
        _validate_path_glob_predicate(predicate)
        where_clauses.append("observations.relative_path GLOB ?")
        parameters.append(predicate.pattern)

    where_clause = " AND\n              ".join(where_clauses)
    return where_clause, tuple(parameters)


def _compile_current_observation_ordering(query: CurrentObservationQuery) -> str:
    """Compile the validated deterministic ordering for row terminals."""
    _validate_sort_expression(query.ordering)
    order_column = _ORDER_COLUMNS[query.ordering.field]
    order_direction = _ORDER_DIRECTIONS[query.ordering.direction]
    tie_breaker = "" if query.ordering.field is _CurrentFileField.PATH else ", observations.relative_path ASC"
    return f"{order_column} {order_direction}{tie_breaker}"


def compile_current_observations(query: CurrentObservationQuery) -> SqliteQuery:
    """Compile one bounded or unbounded current-observation row query."""
    where_clause, parameters = _compile_current_observation_scope(query)
    limit_clause = ""
    if query.limit is not None:
        limit_clause = "LIMIT ?"
        parameters = (*parameters, query.limit)
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
            ORDER BY {_compile_current_observation_ordering(query)}
            {limit_clause}
        """,
        parameters=parameters,
    )


def compile_current_observation_count(query: CurrentObservationQuery) -> SqliteQuery:
    """Compile a complete count that ignores row ordering and limits."""
    where_clause, parameters = _compile_current_observation_scope(query)
    return SqliteQuery(
        statement=f"""
            SELECT COUNT(*) AS count
            FROM file_observations AS observations
            JOIN scan_runs AS scans ON scans.id = observations.scan_id
            JOIN locations ON locations.id = scans.location_id
            WHERE {where_clause}
        """,
        parameters=parameters,
    )


def compile_current_observation_total_size(query: CurrentObservationQuery) -> SqliteQuery:
    """Compile a complete size reduction that ignores row ordering and limits."""
    where_clause, parameters = _compile_current_observation_scope(query)
    return SqliteQuery(
        statement=f"""
            SELECT COALESCE(SUM(observations.size_bytes), 0) AS total_size_bytes
            FROM file_observations AS observations
            JOIN scan_runs AS scans ON scans.id = observations.scan_id
            JOIN locations ON locations.id = scans.location_id
            WHERE {where_clause}
        """,
        parameters=parameters,
    )


def iter_current_observations(
    connection: sqlite3.Connection, query: CurrentObservationQuery
) -> Iterator[FileObservation]:
    """Yield observations directly from one compiled SQLite cursor."""
    compiled = compile_current_observations(query)
    cursor = connection.execute(compiled.statement, compiled.parameters)
    try:
        for row in cursor:
            yield _materialize_file_observation(query.location, row)
    finally:
        cursor.close()


def execute_current_observations(
    connection: sqlite3.Connection, query: CurrentObservationQuery
) -> list[FileObservation]:
    """Execute and materialize one current-observation query."""
    return list(iter_current_observations(connection, query))


def execute_first_current_observation(
    connection: sqlite3.Connection, query: CurrentObservationQuery
) -> FileObservation | None:
    """Return the first matching current observation without a row page."""
    first_query = CurrentObservationQuery(
        location=query.location,
        predicates=query.predicates,
        ordering=query.ordering,
        limit=1,
    )
    compiled = compile_current_observations(first_query)
    cursor = connection.execute(compiled.statement, compiled.parameters)
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    return None if row is None else _materialize_file_observation(query.location, row)


def execute_current_observation_count(connection: sqlite3.Connection, query: CurrentObservationQuery) -> int:
    """Return the complete number of matching current observations."""
    compiled = compile_current_observation_count(query)
    row = connection.execute(compiled.statement, compiled.parameters).fetchone()
    assert row is not None
    return int(row["count"])


def execute_current_observation_total_size(connection: sqlite3.Connection, query: CurrentObservationQuery) -> int:
    """Return the complete logical size of matching current observations."""
    compiled = compile_current_observation_total_size(query)
    row = connection.execute(compiled.statement, compiled.parameters).fetchone()
    assert row is not None
    return int(row["total_size_bytes"])


def _materialize_file_observation(location: Location, row: sqlite3.Row) -> FileObservation:
    """Materialize the common current-observation row projection."""
    return FileObservation(
        location=location,
        relative_path=PurePosixPath(str(row["relative_path"])),
        content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
        size_bytes=int(row["size_bytes"]),
        mtime_ns=int(row["mtime_ns"]),
    )
