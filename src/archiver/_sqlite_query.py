"""SQLite compilation and execution for internal query descriptions."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ._query import (
    ContentQueryDescription,
    CurrentObservationQuery,
    DuplicateGroupQueryDescription,
    TagVocabularyQueryDescription,
)
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
    ScanRun,
    ScanSummary,
    TagProvenance,
    TagUsage,
)
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

_TAG_REGEX_FUNCTION = "archiver_tag_name_regex"
_TAG_ORDER_COLUMNS = {
    _CurrentFileField.TAG_NAME: "tag_name",
    _CurrentFileField.TAG_CONTENT_COUNT: "content_count",
    _CurrentFileField.TAG_ASSERTION_COUNT: "assertion_count",
}


@dataclass(frozen=True, slots=True)
class SqliteQuery:
    """One parameterized SQLite statement ready for execution."""

    statement: str
    parameters: tuple[object, ...]


def _compile_current_observation_scope(query: CurrentObservationQuery) -> tuple[str, tuple[object, ...]]:
    """Compile the shared current-state relation and typed predicates."""
    where_clauses = ["locations.id = ?"]
    parameters: list[object] = [query.location.id]
    if query.scan_id is None:
        where_clauses.append("scans.id = locations.current_scan_id")
    else:
        where_clauses.append("observations.scan_id = ?")
        parameters.append(query.scan_id)
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
                content.digest AS digest,
                content.id AS content_database_id
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
        scan_id=query.scan_id,
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


def execute_active_tag_previews(
    connection: sqlite3.Connection, content_row_ids: tuple[int, ...], *, limit: int | None
) -> tuple[dict[int, int], dict[int, tuple[str, ...]]]:
    """Load distinct active-tag counts/previews for only selected content rows."""
    if not content_row_ids:
        return {}, {}
    placeholders = ", ".join("?" for _ in content_row_ids)
    limit_clause = "" if limit is None else "WHERE tag_rank <= ?"
    parameters: tuple[object, ...] = content_row_ids if limit is None else (*content_row_ids, limit)
    cursor = connection.execute(
        f"""
        WITH distinct_active_tags AS (
            SELECT DISTINCT assertions.content_id AS content_row_id, tags.name AS tag_name
            FROM content_tag_assertions AS assertions
            JOIN tags ON tags.id = assertions.tag_id
            WHERE assertions.retracted_at_ns IS NULL
              AND assertions.content_id IN ({placeholders})
        ),
        ranked_tags AS (
            SELECT content_row_id, tag_name,
                   COUNT(*) OVER (PARTITION BY content_row_id) AS active_tag_count,
                   ROW_NUMBER() OVER (PARTITION BY content_row_id ORDER BY tag_name) AS tag_rank
            FROM distinct_active_tags
        )
        SELECT content_row_id, tag_name, active_tag_count
        FROM ranked_tags
        {limit_clause}
        ORDER BY content_row_id, tag_name
        """,
        parameters,
    )
    counts = {row_id: 0 for row_id in content_row_ids}
    previews: dict[int, list[str]] = {row_id: [] for row_id in content_row_ids}
    try:
        for row in cursor:
            row_id = int(row["content_row_id"])
            counts[row_id] = int(row["active_tag_count"])
            previews[row_id].append(str(row["tag_name"]))
    finally:
        cursor.close()
    return counts, {row_id: tuple(names) for row_id, names in previews.items()}


def execute_current_observation_tag_views(
    connection: sqlite3.Connection, query: CurrentObservationQuery, *, tag_limit: int | None
) -> tuple[CurrentFileTagView, ...]:
    """Materialize bounded observations, then previews for selected identities."""
    compiled = compile_current_observations(query)
    cursor = connection.execute(compiled.statement, compiled.parameters)
    try:
        rows = tuple(cursor)
    finally:
        cursor.close()
    content_row_ids = tuple(dict.fromkeys(int(row["content_database_id"]) for row in rows))
    counts, previews = execute_active_tag_previews(connection, content_row_ids, limit=tag_limit)
    return tuple(
        CurrentFileTagView(
            observation=_materialize_file_observation(query.location, row),
            active_tag_count=counts[int(row["content_database_id"])],
            tags=previews[int(row["content_database_id"])],
        )
        for row in rows
    )


def _compile_content_scope(query: ContentQueryDescription) -> tuple[str, tuple[object, ...]]:
    clauses = ["1 = 1"]
    parameters: list[object] = []
    for predicate in query.predicates:
        if type(predicate) is ContentPredicate:
            clauses.append("content.algorithm = ? AND content.digest = ?")
            parameters.extend((predicate.content_id.algorithm, predicate.content_id.digest))
        else:
            assert type(predicate) is TagPredicate
            selection, tag_parameters = _compile_active_tag_match(predicate)
            clauses.append(f"content.id IN ({selection})")
            parameters.extend(tag_parameters)
    return " AND\n              ".join(clauses), tuple(parameters)


def compile_contents(query: ContentQueryDescription) -> SqliteQuery:
    """Compile bounded catalog-wide content selection."""
    where_clause, parameters = _compile_content_scope(query)
    limit_clause = "" if query.limit is None else "LIMIT ?"
    if query.limit is not None:
        parameters = (*parameters, query.limit)
    return SqliteQuery(
        f"""
        SELECT content.id AS content_database_id, content.algorithm, content.digest, content.size_bytes
        FROM content
        WHERE {where_clause}
        ORDER BY content.algorithm ASC, content.digest ASC
        {limit_clause}
        """,
        parameters,
    )


def execute_contents(connection: sqlite3.Connection, query: ContentQueryDescription) -> tuple[ContentRecord, ...]:
    compiled = compile_contents(query)
    cursor = connection.execute(compiled.statement, compiled.parameters)
    try:
        return tuple(
            ContentRecord(
                ContentId(str(row["algorithm"]), str(row["digest"])),
                int(row["size_bytes"]),
            )
            for row in cursor
        )
    finally:
        cursor.close()


def execute_content_count(connection: sqlite3.Connection, query: ContentQueryDescription) -> int:
    where_clause, parameters = _compile_content_scope(query)
    cursor = connection.execute(f"SELECT COUNT(*) AS count FROM content WHERE {where_clause}", parameters)
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return int(row["count"])


def _selected_content_rows(connection: sqlite3.Connection, query: ContentQueryDescription) -> tuple[sqlite3.Row, ...]:
    compiled = compile_contents(query)
    cursor = connection.execute(compiled.statement, compiled.parameters)
    try:
        return tuple(cursor)
    finally:
        cursor.close()


def execute_content_details(
    connection: sqlite3.Connection,
    query: ContentQueryDescription,
    *,
    location: Location | None,
    scan_id: int | None,
    path_limit: int,
    tag_limit: int | None,
) -> tuple[ContentTagView, ...]:
    """Materialize bounded content parents and selected-only previews."""
    rows = _selected_content_rows(connection, query)
    row_ids = tuple(int(row["content_database_id"]) for row in rows)
    path_counts = {row_id: 0 for row_id in row_ids}
    paths: dict[int, list[PurePosixPath]] = {row_id: [] for row_id in row_ids}
    if row_ids and location is not None and scan_id is not None:
        placeholders = ", ".join("?" for _ in row_ids)
        cursor = connection.execute(
            f"""
            WITH ranked_paths AS (
                SELECT observations.content_id AS content_row_id,
                       observations.relative_path,
                       COUNT(*) OVER (PARTITION BY observations.content_id) AS current_path_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY observations.content_id ORDER BY observations.relative_path
                       ) AS path_rank
                FROM file_observations AS observations
                WHERE observations.scan_id = ?
                  AND observations.content_id IN ({placeholders})
            )
            SELECT content_row_id, relative_path, current_path_count
            FROM ranked_paths
            WHERE path_rank <= ?
            ORDER BY content_row_id, relative_path
            """,
            (scan_id, *row_ids, path_limit),
        )
        try:
            for row in cursor:
                row_id = int(row["content_row_id"])
                path_counts[row_id] = int(row["current_path_count"])
                paths[row_id].append(PurePosixPath(str(row["relative_path"])))
        finally:
            cursor.close()
    tag_counts, tag_previews = execute_active_tag_previews(connection, row_ids, limit=tag_limit)
    return tuple(
        ContentTagView(
            content_id=ContentId(str(row["algorithm"]), str(row["digest"])),
            size_bytes=int(row["size_bytes"]),
            current_path_count=path_counts[int(row["content_database_id"])],
            current_paths=tuple(paths[int(row["content_database_id"])]),
            active_tag_count=tag_counts[int(row["content_database_id"])],
            tags=tag_previews[int(row["content_database_id"])],
        )
        for row in rows
    )


def execute_content_current_path_count(
    connection: sqlite3.Connection, query: ContentQueryDescription, *, scan_id: int | None
) -> int:
    if scan_id is None:
        return 0
    where_clause, parameters = _compile_content_scope(query)
    cursor = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM file_observations AS observations
        JOIN content ON content.id = observations.content_id
        WHERE observations.scan_id = ? AND {where_clause}
        """,
        (scan_id, *parameters),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return int(row["count"])


def _compile_tag_vocabulary(query: TagVocabularyQueryDescription, *, count: bool) -> SqliteQuery:
    provenance_clause = ""
    parameters: list[object] = []
    if query.provenance is not None:
        provenance_clause = "AND assertions.provenance_kind = ?"
        parameters.append(query.provenance)
    regex_clause = ""
    for predicate in query.predicates:
        regex_clause += f" AND {_TAG_REGEX_FUNCTION}(?, tag_name)"
        parameters.append(predicate.source)
    usage_cte = f"""
        WITH tag_usage AS (
            SELECT tags.name AS tag_name,
                   COUNT(DISTINCT assertions.content_id) AS content_count,
                   COUNT(*) AS assertion_count,
                   SUM(assertions.provenance_kind = 'user') AS user_assertion_count,
                   SUM(assertions.provenance_kind = 'system') AS system_assertion_count
            FROM content_tag_assertions AS assertions
            JOIN tags ON tags.id = assertions.tag_id
            WHERE assertions.retracted_at_ns IS NULL {provenance_clause}
            GROUP BY tags.id, tags.name
        )
    """
    if count:
        return SqliteQuery(
            f"{usage_cte} SELECT COUNT(*) AS count FROM tag_usage WHERE 1 = 1 {regex_clause}",
            tuple(parameters),
        )
    column = _TAG_ORDER_COLUMNS[query.ordering.field]
    direction = _ORDER_DIRECTIONS[query.ordering.direction]
    tie = "" if query.ordering.field is _CurrentFileField.TAG_NAME else ", tag_name ASC"
    limit_clause = "" if query.limit is None else "LIMIT ?"
    if query.limit is not None:
        parameters.append(query.limit)
    return SqliteQuery(
        f"""
        {usage_cte}
        SELECT tag_name, content_count, assertion_count, user_assertion_count, system_assertion_count
        FROM tag_usage
        WHERE 1 = 1 {regex_clause}
        ORDER BY {column} {direction}{tie}
        {limit_clause}
        """,
        tuple(parameters),
    )


def _materialize_tag_usage(row: sqlite3.Row) -> TagUsage:
    return TagUsage(
        tag=str(row["tag_name"]),
        content_count=int(row["content_count"]),
        assertion_count=int(row["assertion_count"]),
        user_assertion_count=int(row["user_assertion_count"]),
        system_assertion_count=int(row["system_assertion_count"]),
    )


def _with_tag_regex_function(connection: sqlite3.Connection) -> None:
    def matches(source: str, name: str) -> int:
        return int(re.search(source, name) is not None)

    connection.create_function(_TAG_REGEX_FUNCTION, 2, matches, deterministic=True)


def execute_tag_vocabulary(
    connection: sqlite3.Connection, query: TagVocabularyQueryDescription
) -> tuple[TagUsage, ...]:
    compiled = _compile_tag_vocabulary(query, count=False)
    registered = bool(query.predicates)
    if registered:
        _with_tag_regex_function(connection)
    cursor: sqlite3.Cursor | None = None
    try:
        cursor = connection.execute(compiled.statement, compiled.parameters)
        return tuple(_materialize_tag_usage(row) for row in cursor)
    finally:
        if cursor is not None:
            cursor.close()
        if registered:
            connection.create_function(_TAG_REGEX_FUNCTION, 2, None)


def execute_tag_vocabulary_count(connection: sqlite3.Connection, query: TagVocabularyQueryDescription) -> int:
    compiled = _compile_tag_vocabulary(query, count=True)
    registered = bool(query.predicates)
    if registered:
        _with_tag_regex_function(connection)
    cursor: sqlite3.Cursor | None = None
    try:
        cursor = connection.execute(compiled.statement, compiled.parameters)
        row = cursor.fetchone()
        assert row is not None
        return int(row["count"])
    finally:
        if cursor is not None:
            cursor.close()
        if registered:
            connection.create_function(_TAG_REGEX_FUNCTION, 2, None)


def _materialize_scan_run(row: sqlite3.Row) -> ScanRun:
    completed_at_ns = row["completed_at_ns"]
    return ScanRun(
        id=int(row["scan_id"]),
        location=Location(int(row["location_id"]), Path(str(row["root_path"]))),
        status=str(row["scan_status"]),
        started_at_ns=int(row["started_at_ns"]),
        completed_at_ns=None if completed_at_ns is None else int(completed_at_ns),
    )


_SCAN_PROJECTION = """
    scans.id AS scan_id,
    locations.id AS location_id,
    locations.root_path AS root_path,
    scans.status AS scan_status,
    scans.started_at_ns AS started_at_ns,
    scans.completed_at_ns AS completed_at_ns
"""


def execute_current_scan(connection: sqlite3.Connection, location: Location) -> ScanRun | None:
    cursor = connection.execute(
        f"""
        SELECT {_SCAN_PROJECTION}
        FROM locations
        JOIN scan_runs AS scans ON scans.id = locations.current_scan_id
        WHERE locations.id = ?
        """,
        (location.id,),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    return None if row is None else _materialize_scan_run(row)


def execute_scan_summary(connection: sqlite3.Connection, scan_id: int) -> ScanSummary:
    cursor = connection.execute(
        """
        SELECT COUNT(*) AS files_observed,
               COALESCE(SUM(size_bytes), 0) AS total_bytes_observed,
               COUNT(DISTINCT content_id) AS distinct_content_count,
               COUNT(DISTINCT CASE WHEN duplicate_group = 1 THEN content_id END)
                   AS duplicate_content_group_count
        FROM (
            SELECT observations.*,
                   CASE WHEN COUNT(*) OVER (PARTITION BY content_id) > 1 THEN 1 ELSE 0 END AS duplicate_group
            FROM file_observations AS observations
            WHERE scan_id = ?
        )
        """,
        (scan_id,),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return ScanSummary(
        int(row["files_observed"]),
        int(row["total_bytes_observed"]),
        int(row["distinct_content_count"]),
        int(row["duplicate_content_group_count"]),
    )


def iter_scan_history(connection: sqlite3.Connection) -> Iterator[ScanRun]:
    cursor = connection.execute(
        f"""
        SELECT {_SCAN_PROJECTION}
        FROM scan_runs AS scans
        JOIN locations ON locations.id = scans.location_id
        ORDER BY locations.root_path, scans.id
        """
    )
    try:
        for row in cursor:
            yield _materialize_scan_run(row)
    finally:
        cursor.close()


def iter_observation_history(connection: sqlite3.Connection) -> Iterator[HistoricalObservation]:
    cursor = connection.execute(
        f"""
        SELECT {_SCAN_PROJECTION},
               observations.relative_path, observations.size_bytes, observations.mtime_ns,
               content.algorithm, content.digest
        FROM file_observations AS observations
        JOIN scan_runs AS scans ON scans.id = observations.scan_id
        JOIN locations ON locations.id = scans.location_id
        JOIN content ON content.id = observations.content_id
        ORDER BY locations.root_path, scans.id, observations.relative_path
        """
    )
    try:
        for row in cursor:
            yield HistoricalObservation(
                scan=_materialize_scan_run(row),
                relative_path=PurePosixPath(str(row["relative_path"])),
                content_id=ContentId(str(row["algorithm"]), str(row["digest"])),
                size_bytes=int(row["size_bytes"]),
                mtime_ns=int(row["mtime_ns"]),
            )
    finally:
        cursor.close()


def execute_content_record(connection: sqlite3.Connection, content_id: ContentId) -> tuple[int, int] | None:
    cursor = connection.execute(
        "SELECT id, size_bytes FROM content WHERE algorithm = ? AND digest = ?",
        (content_id.algorithm, content_id.digest),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    return None if row is None else (int(row["id"]), int(row["size_bytes"]))


def execute_content_for_current_path(
    connection: sqlite3.Connection, location: Location, relative_path: PurePosixPath
) -> ContentId | None:
    cursor = connection.execute(
        """
        SELECT content.algorithm, content.digest
        FROM file_observations AS observations
        JOIN content ON content.id = observations.content_id
        WHERE observations.scan_id = (
            SELECT current_scan_id FROM locations WHERE id = ?
        ) AND observations.relative_path = ?
        """,
        (location.id, relative_path.as_posix()),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    return None if row is None else ContentId(str(row["algorithm"]), str(row["digest"]))


def execute_tags_for_content(
    connection: sqlite3.Connection, content_row_id: int, content_id: ContentId
) -> tuple[ContentTagAssertion, ...]:
    cursor = connection.execute(
        """
        SELECT tags.name AS tag_name, assertions.provenance_kind,
               assertions.source_name, assertions.source_version,
               assertions.source_detail, assertions.asserted_at_ns
        FROM content_tag_assertions AS assertions
        JOIN tags ON tags.id = assertions.tag_id
        WHERE assertions.content_id = ? AND assertions.retracted_at_ns IS NULL
        ORDER BY tags.name, assertions.provenance_kind, assertions.source_name,
                 assertions.source_version, assertions.source_detail, assertions.asserted_at_ns
        """,
        (content_row_id,),
    )
    try:
        return tuple(
            ContentTagAssertion(
                content_id=content_id,
                tag=str(row["tag_name"]),
                provenance=TagProvenance(
                    kind=str(row["provenance_kind"]),  # type: ignore[arg-type]
                    source_name=str(row["source_name"]),
                    source_version=str(row["source_version"]),
                    source_detail=str(row["source_detail"]),
                ),
                asserted_at_ns=int(row["asserted_at_ns"]),
            )
            for row in cursor
        )
    finally:
        cursor.close()


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
