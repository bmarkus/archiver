"""SQLite compilation and execution for internal query descriptions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

from ._query import CurrentObservationQuery, DuplicateGroupQueryDescription
from .models import ContentId, DuplicateGroupView, DuplicateSummary, FileObservation, Location
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


def _compile_duplicate_scope(query: DuplicateGroupQueryDescription) -> tuple[str, tuple[object, ...]]:
    """Compile the current-scan scope shared by duplicate statements."""
    return "observations.scan_id = ?", (query.scan_id,)


def compile_duplicate_groups(query: DuplicateGroupQueryDescription) -> SqliteQuery:
    """Compile bounded duplicate group selection with complete metrics."""
    where_clause, parameters = _compile_duplicate_scope(query)
    return SqliteQuery(
        statement=f"""
            SELECT
                content.id AS content_database_id,
                content.algorithm AS algorithm,
                content.digest AS digest,
                content.size_bytes AS size_bytes,
                COUNT(*) AS file_instance_count,
                (COUNT(*) - 1) * content.size_bytes AS potential_redundant_bytes
            FROM file_observations AS observations
            JOIN content ON content.id = observations.content_id
            WHERE {where_clause}
            GROUP BY content.id, content.algorithm, content.digest, content.size_bytes
            HAVING COUNT(*) > 1
            ORDER BY potential_redundant_bytes DESC, content.algorithm ASC, content.digest ASC
            LIMIT ?
        """,
        parameters=(*parameters, query.group_limit),
    )


def compile_duplicate_group_members(query: DuplicateGroupQueryDescription, content_database_id: int) -> SqliteQuery:
    """Compile bounded, ordered members for one selected duplicate group."""
    if type(content_database_id) is not int:
        raise TypeError("duplicate member content ID must be an integer")
    if content_database_id < 1:
        raise ValueError("duplicate member content ID must be positive")
    where_clause, parameters = _compile_duplicate_scope(query)
    return SqliteQuery(
        statement=f"""
            SELECT
                observations.relative_path AS relative_path,
                observations.size_bytes AS size_bytes,
                observations.mtime_ns AS mtime_ns,
                content.algorithm AS algorithm,
                content.digest AS digest
            FROM file_observations AS observations
            JOIN content ON content.id = observations.content_id
            WHERE {where_clause} AND observations.content_id = ?
            ORDER BY observations.relative_path ASC
            LIMIT ?
        """,
        parameters=(*parameters, content_database_id, query.member_limit),
    )


def compile_duplicate_summary(query: DuplicateGroupQueryDescription) -> SqliteQuery:
    """Compile complete duplicate metrics independent of display bounds."""
    where_clause, parameters = _compile_duplicate_scope(query)
    return SqliteQuery(
        statement=f"""
            SELECT
                COUNT(*) AS duplicate_content_group_count,
                COALESCE(SUM(file_instance_count), 0) AS duplicate_file_instance_count,
                COALESCE(SUM((file_instance_count - 1) * size_bytes), 0) AS potential_redundant_bytes
            FROM (
                SELECT
                    observations.content_id AS content_database_id,
                    MAX(content.size_bytes) AS size_bytes,
                    COUNT(*) AS file_instance_count
                FROM file_observations AS observations
                JOIN content ON content.id = observations.content_id
                WHERE {where_clause}
                GROUP BY observations.content_id
                HAVING COUNT(*) > 1
            ) AS duplicate_groups
        """,
        parameters=parameters,
    )


def execute_duplicate_groups(
    connection: sqlite3.Connection, query: DuplicateGroupQueryDescription
) -> tuple[DuplicateGroupView, ...]:
    """Execute one bounded duplicate query pinned to one current scan."""
    compiled_groups = compile_duplicate_groups(query)
    group_rows = connection.execute(compiled_groups.statement, compiled_groups.parameters).fetchall()
    groups: list[DuplicateGroupView] = []
    for row in group_rows:
        compiled_members = compile_duplicate_group_members(query, int(row["content_database_id"]))
        member_rows = connection.execute(compiled_members.statement, compiled_members.parameters).fetchall()
        groups.append(
            DuplicateGroupView(
                content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
                size_bytes=int(row["size_bytes"]),
                file_instance_count=int(row["file_instance_count"]),
                potential_redundant_bytes=int(row["potential_redundant_bytes"]),
                members=tuple(_materialize_file_observation(query.location, member) for member in member_rows),
            )
        )
    return tuple(groups)


def execute_duplicate_summary(
    connection: sqlite3.Connection, query: DuplicateGroupQueryDescription
) -> DuplicateSummary:
    """Execute complete duplicate metrics for one resolved current scan."""
    compiled = compile_duplicate_summary(query)
    row = connection.execute(compiled.statement, compiled.parameters).fetchone()
    assert row is not None
    return DuplicateSummary(
        duplicate_content_group_count=int(row["duplicate_content_group_count"]),
        duplicate_file_instance_count=int(row["duplicate_file_instance_count"]),
        potential_redundant_bytes=int(row["potential_redundant_bytes"]),
    )


def _materialize_file_observation(location: Location, row: sqlite3.Row) -> FileObservation:
    """Materialize the common current-observation row projection."""
    return FileObservation(
        location=location,
        relative_path=PurePosixPath(str(row["relative_path"])),
        content_id=ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"])),
        size_bytes=int(row["size_bytes"]),
        mtime_ns=int(row["mtime_ns"]),
    )
