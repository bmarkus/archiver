"""Validation and SQL boundary coverage for duplicate offsets."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from archiver import Catalog, ContentId, Location
from archiver._query import DuplicateGroupQueryDescription
from archiver._sqlite_query import compile_duplicate_group_members, compile_duplicate_groups, compile_duplicate_summary
from archiver.query import duplicate, size


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (True, TypeError),
        (False, TypeError),
        (1.5, TypeError),
        ("1", TypeError),
        (None, TypeError),
        (-1, ValueError),
        (2**63, ValueError),
    ],
)
def test_offsets_validate_eagerly_in_public_and_private_queries(
    tmp_path: Path,
    value: object,
    error: type[Exception],
) -> None:
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        query = catalog.duplicates(tmp_path)
        with pytest.raises(error):
            query.offset(cast(int, value))
        with pytest.raises(error):
            replace(query, group_offset=cast(int, value))
    with pytest.raises(error):
        DuplicateGroupQueryDescription(Location(1, tmp_path), 1, size.asc(), 2, 3, cast(int, value))


def test_offset_transformations_are_immutable_and_do_not_read_catalog(tmp_path: Path) -> None:
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        base = catalog.duplicates(tmp_path)
    # Even a closed catalog allows validated description-only transformations.
    derived = base.offset(10).limit(7).member_limit(1).sort(size.asc())
    assert base.group_offset == 0
    assert derived.group_offset == 10
    assert derived.group_limit == 7 and derived.member_row_limit == 1
    assert derived == base.sort(size.asc()).member_limit(1).limit(7).offset(10)
    assert derived.offset(3).offset(0) == derived.offset(0)
    assert derived.offset(2**63 - 1).group_offset == 2**63 - 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        derived.fetch()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        tuple(derived.stream())


@pytest.mark.parametrize("limit", [None, 3])
@pytest.mark.parametrize("offset", [10, 2**63 - 1])
def test_sql_offset_is_parameterized_and_does_not_affect_members_or_summary(
    tmp_path: Path,
    limit: int | None,
    offset: int,
) -> None:
    description = DuplicateGroupQueryDescription(
        Location(1, tmp_path), 7, duplicate.file_count.desc(), limit, 2, offset
    )
    compiled = compile_duplicate_groups(description)
    assert "LIMIT ? OFFSET ?" in compiled.statement
    assert compiled.parameters == (7, -1 if limit is None else limit, offset)
    assert compiled.statement.index("ORDER BY") < compiled.statement.index("OFFSET")
    content_id = ContentId("sha256", "a" * 64)
    members = compile_duplicate_group_members(description, content_id)
    summary = compile_duplicate_summary(description)
    assert "OFFSET" not in members.statement and "OFFSET" not in summary.statement
    assert "LIMIT" not in summary.statement
    assert members.parameters == (7, "sha256", "a" * 64, 2)
    assert summary.parameters == (7,)
