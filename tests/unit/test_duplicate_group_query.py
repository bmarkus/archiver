from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from archiver import Catalog, ContentId, DuplicateGroupQuery
from archiver._query import DuplicateGroupQueryDescription
from archiver._sqlite_query import (
    compile_duplicate_group_members,
    compile_duplicate_groups,
    compile_duplicate_summary,
    iter_duplicate_group_members,
    iter_duplicate_group_rows,
)
from archiver.models import Location
from archiver.query import duplicate, mtime, path, size


def test_public_duplicate_queries_are_immutable_semantic_values(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    first_catalog = Catalog.create(tmp_path / "first.sqlite")
    second_catalog = Catalog.create(tmp_path / "second.sqlite")
    try:
        base = first_catalog.duplicates(root)
        derived = base.limit(7).member_limit(3)
        same_value = second_catalog.duplicates(root).limit(7).member_limit(3)

        assert isinstance(base, DuplicateGroupQuery)
        assert base.root == root.resolve()
        assert base.group_limit == 20
        assert base.member_row_limit == 20
        assert derived.group_limit == 7
        assert derived.member_row_limit == 3
        assert derived == same_value
        assert "Catalog" not in repr(derived)
        with pytest.raises(FrozenInstanceError):
            setattr(derived, "group_limit", 99)
    finally:
        first_catalog.close()
        second_catalog.close()


def test_internal_duplicate_description_requires_resolved_domain_values(tmp_path: Path) -> None:
    root = tmp_path / "source"
    persisted = Location(id=7, root=root)

    with pytest.raises(ValueError, match="persisted location"):
        DuplicateGroupQueryDescription(Location(id=None, root=root), 1, size.asc(), 2, 3)
    with pytest.raises(TypeError, match="scan ID"):
        DuplicateGroupQueryDescription(persisted, True, size.asc(), 2, 3)
    with pytest.raises(ValueError, match="scan ID"):
        DuplicateGroupQueryDescription(persisted, 0, size.asc(), 2, 3)
    with pytest.raises(ValueError, match="at least 1"):
        DuplicateGroupQueryDescription(persisted, 1, size.asc(), 0, 3)
    with pytest.raises(TypeError, match="integer"):
        DuplicateGroupQueryDescription(persisted, 1, size.asc(), 2, 3.5)  # type: ignore[arg-type]


def test_duplicate_compilers_share_one_scan_and_apply_sql_bounds(tmp_path: Path) -> None:
    query = DuplicateGroupQueryDescription(
        location=Location(id=7, root=tmp_path / "source"),
        scan_id=11,
        ordering=duplicate.file_count.asc(),
        group_limit=3,
        member_limit=2,
    )

    groups = compile_duplicate_groups(query)
    content_id = ContentId("sha256", "a" * 64)
    members = compile_duplicate_group_members(query, content_id)
    summary = compile_duplicate_summary(query)

    assert groups.parameters == (11, 3)
    assert members.parameters == (11, "sha256", "a" * 64, 2)
    assert summary.parameters == (11,)
    assert groups.statement.count("?") == 2
    assert members.statement.count("?") == 4
    assert summary.statement.count("?") == 1
    assert "GROUP BY" in groups.statement
    assert "HAVING COUNT(*) > 1" in groups.statement
    assert "file_instance_count ASC" in groups.statement
    assert "content.algorithm ASC, content.digest ASC" in groups.statement
    assert "LIMIT ?" in groups.statement
    assert "ORDER BY observations.relative_path ASC" in members.statement
    assert "LIMIT ?" in members.statement
    assert "GROUP BY observations.content_id" in summary.statement
    assert "ORDER BY" not in summary.statement
    assert "LIMIT" not in summary.statement


def test_member_compiler_rejects_invalid_content_identity(tmp_path: Path) -> None:
    query = DuplicateGroupQueryDescription(Location(id=7, root=tmp_path), 11, size.asc(), 3, 2)

    with pytest.raises(TypeError, match="ContentId"):
        compile_duplicate_group_members(query, True)  # type: ignore[arg-type]


def test_duplicate_sorting_is_typed_replacing_and_domain_specific(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        base = catalog.duplicates(root)
        by_size = base.sort(size.asc())
        by_count = by_size.sort(duplicate.file_count.desc())

        assert base.ordering == duplicate.redundant_bytes.desc()
        assert by_size.ordering == size.asc()
        assert by_count.ordering == duplicate.file_count.desc()
        assert base.ordering == duplicate.redundant_bytes.desc()

        with pytest.raises(TypeError, match="duplicate-group sort expression"):
            base.sort("size")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="duplicate-group sort expression"):
            base.sort(lambda value: value)  # type: ignore[arg-type]
        for invalid in (path.asc(), mtime.desc()):
            with pytest.raises(ValueError, match="duplicate-group sort field"):
                base.sort(invalid)
        with pytest.raises(ValueError, match="current-file sort field"):
            catalog.files(root).sort(duplicate.file_count.asc())


def test_duplicate_compilers_support_explicit_unbounded_selection(tmp_path: Path) -> None:
    query = DuplicateGroupQueryDescription(
        Location(id=7, root=tmp_path),
        11,
        duplicate.redundant_bytes.desc(),
        None,
        None,
    )
    groups = compile_duplicate_groups(query)
    members = compile_duplicate_group_members(query, ContentId("sha256", "a" * 64))

    assert groups.parameters == (11,)
    assert members.parameters == (11, "sha256", "a" * 64)
    assert "LIMIT" not in groups.statement
    assert "LIMIT" not in members.statement


class _TrackingCursor(Iterator[object]):
    def __init__(self, rows: list[object], *, fail: bool = False) -> None:
        self._rows = iter(rows)
        self._fail = fail
        self.closed = False

    def __iter__(self) -> _TrackingCursor:
        return self

    def __next__(self) -> object:
        if self._fail:
            raise RuntimeError("controlled cursor failure")
        return next(self._rows)

    def close(self) -> None:
        self.closed = True


class _TrackingConnection:
    def __init__(self, cursor: _TrackingCursor) -> None:
        self.cursor = cursor

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _TrackingCursor:
        assert statement
        assert parameters
        return self.cursor


def test_duplicate_iterators_close_cursors_on_close_exhaustion_and_error(tmp_path: Path) -> None:
    location = Location(id=7, root=tmp_path)
    query = DuplicateGroupQueryDescription(location, 11, size.asc(), None, None)

    explicit_cursor = _TrackingCursor([cast(sqlite3.Row, object()), cast(sqlite3.Row, object())])
    explicit = iter_duplicate_group_rows(cast(sqlite3.Connection, _TrackingConnection(explicit_cursor)), query)
    next(explicit)
    explicit.close()
    assert explicit_cursor.closed

    member_row = {
        "relative_path": "nested/a.bin",
        "size_bytes": 1,
        "mtime_ns": 2,
        "algorithm": "sha256",
        "digest": "a" * 64,
    }
    exhausted_cursor = _TrackingCursor([cast(sqlite3.Row, member_row)])
    members = iter_duplicate_group_members(
        cast(sqlite3.Connection, _TrackingConnection(exhausted_cursor)),
        query,
        ContentId("sha256", "a" * 64),
    )
    assert len(tuple(members)) == 1
    assert exhausted_cursor.closed

    failing_cursor = _TrackingCursor([], fail=True)
    failing = iter_duplicate_group_rows(cast(sqlite3.Connection, _TrackingConnection(failing_cursor)), query)
    with pytest.raises(RuntimeError, match="controlled cursor failure"):
        next(failing)
    assert failing_cursor.closed
