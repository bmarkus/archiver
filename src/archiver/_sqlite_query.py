"""SQLite compilation and execution for internal query descriptions."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import PurePosixPath

from ._query import CurrentObservationQuery, DuplicateGroupQueryDescription
from .models import ContentId, DuplicateGroupView, DuplicateSummary, FileObservation, Location
from .query import (
    ContentPredicate,
    PathGlobPredicate,
    TagPredicate,
    _CurrentFileField,
    _SortDirection,
    _TagMatchMode,
    _validate_current_file_predicate,
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

_DUPLICATE_ORDER_COLUMNS = {
    _CurrentFileField.SIZE: "content.size_bytes",
    _CurrentFileField.DUPLICATE_FILE_COUNT: "file_instance_count",
    _CurrentFileField.DUPLICATE_REDUNDANT_BYTES: "potential_redundant_bytes",
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
        _validate_current_file_predicate(predicate)
        if type(predicate) is PathGlobPredicate:
            where_clauses.append("observations.relative_path GLOB ?")
            parameters.append(predicate.pattern)
        elif type(predicate) is ContentPredicate:
            where_clauses.append(
                """EXISTS (
                    SELECT 1
                    FROM content AS matching_content
                    WHERE matching_content.id = observations.content_id
                      AND matching_content.algorithm = ?
                      AND matching_content.digest = ?
                )"""
            )
            parameters.extend((predicate.content_id.algorithm, predicate.content_id.digest))
        else:
            assert type(predicate) is TagPredicate
            tag_selection, tag_parameters = _compile_active_tag_match(predicate)
            where_clauses.append(f"observations.content_id IN ({tag_selection})")
            parameters.extend(tag_parameters)

    where_clause = " AND\n              ".join(where_clauses)
    return where_clause, tuple(parameters)


def _compile_active_tag_match(predicate: TagPredicate) -> tuple[str, tuple[object, ...]]:
    """Compile distinct content IDs matching one active-tag predicate."""
    placeholders = ", ".join("?" for _ in predicate.names)
    provenance_clause = ""
    parameters: list[object] = [*predicate.names]
    if predicate.provenance is not None:
        provenance_clause = "AND matching_assertions.provenance_kind = ?"
        parameters.append(predicate.provenance)
    required_count = len(predicate.names) if predicate.match is _TagMatchMode.ALL else 1
    parameters.append(required_count)
    return (
        f"""SELECT matching_assertions.content_id
                FROM content_tag_assertions AS matching_assertions
                JOIN tags AS matching_tags ON matching_tags.id = matching_assertions.tag_id
                WHERE matching_assertions.retracted_at_ns IS NULL
                  AND matching_tags.name IN ({placeholders})
                  {provenance_clause}
                GROUP BY matching_assertions.content_id
                HAVING COUNT(DISTINCT matching_tags.name) >= ?""",
        tuple(parameters),
    )


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


def compile_current_observation_distinct_content_count(query: CurrentObservationQuery) -> SqliteQuery:
    """Compile a complete distinct-content count ignoring ordering and limits."""
    where_clause, parameters = _compile_current_observation_scope(query)
    return SqliteQuery(
        statement=f"""
            SELECT COUNT(DISTINCT observations.content_id) AS distinct_content_count
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


def execute_current_observation_distinct_content_count(
    connection: sqlite3.Connection, query: CurrentObservationQuery
) -> int:
    """Return the complete number of matching current content identities."""
    compiled = compile_current_observation_distinct_content_count(query)
    row = connection.execute(compiled.statement, compiled.parameters).fetchone()
    assert row is not None
    return int(row["distinct_content_count"])


def _compile_duplicate_scope(query: DuplicateGroupQueryDescription) -> tuple[str, tuple[object, ...]]:
    """Compile the current-scan scope shared by duplicate statements."""
    return "observations.scan_id = ?", (query.scan_id,)


def compile_duplicate_groups(query: DuplicateGroupQueryDescription) -> SqliteQuery:
    """Compile duplicate group selection with complete metrics."""
    where_clause, parameters = _compile_duplicate_scope(query)
    order_column = _DUPLICATE_ORDER_COLUMNS[query.ordering.field]
    direction = _ORDER_DIRECTIONS[query.ordering.direction]
    limit_clause = "" if query.group_limit is None else "LIMIT ?"
    limit_parameters: tuple[object, ...] = () if query.group_limit is None else (query.group_limit,)
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
            ORDER BY {order_column} {direction}, content.algorithm ASC, content.digest ASC
            {limit_clause}
        """,
        parameters=(*parameters, *limit_parameters),
    )


def compile_duplicate_group_members(query: DuplicateGroupQueryDescription, content_id: ContentId) -> SqliteQuery:
    """Compile ordered members for one selected duplicate group."""
    if type(content_id) is not ContentId:
        raise TypeError("duplicate members require a ContentId")
    where_clause, parameters = _compile_duplicate_scope(query)
    limit_clause = "" if query.member_limit is None else "LIMIT ?"
    limit_parameters: tuple[object, ...] = () if query.member_limit is None else (query.member_limit,)
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
            WHERE {where_clause}
              AND content.algorithm = ?
              AND content.digest = ?
            ORDER BY observations.relative_path ASC
            {limit_clause}
        """,
        parameters=(*parameters, content_id.algorithm, content_id.digest, *limit_parameters),
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
    groups: list[DuplicateGroupView] = []
    with closing(iter_duplicate_group_rows(connection, query)) as rows:
        for row in rows:
            content_id = ContentId(algorithm=str(row["algorithm"]), digest=str(row["digest"]))
            groups.append(
                DuplicateGroupView(
                    content_id=content_id,
                    size_bytes=int(row["size_bytes"]),
                    file_instance_count=int(row["file_instance_count"]),
                    potential_redundant_bytes=int(row["potential_redundant_bytes"]),
                    members=tuple(iter_duplicate_group_members(connection, query, content_id)),
                )
            )
    return tuple(groups)


def iter_duplicate_group_rows(
    connection: sqlite3.Connection, query: DuplicateGroupQueryDescription
) -> Generator[sqlite3.Row, None, None]:
    """Yield duplicate group rows and close the cursor reliably."""
    compiled = compile_duplicate_groups(query)
    cursor = connection.execute(compiled.statement, compiled.parameters)
    try:
        yield from cursor
    finally:
        cursor.close()


def iter_duplicate_group_members(
    connection: sqlite3.Connection,
    query: DuplicateGroupQueryDescription,
    content_id: ContentId,
) -> Generator[FileObservation, None, None]:
    """Yield members for one pinned group and close its cursor reliably."""
    compiled = compile_duplicate_group_members(query, content_id)
    cursor = connection.execute(compiled.statement, compiled.parameters)
    try:
        for row in cursor:
            yield _materialize_file_observation(query.location, row)
    finally:
        cursor.close()


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
