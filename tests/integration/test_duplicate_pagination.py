"""Paged duplicate selection, refresh semantics, and read safety."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from archiver import (
    Catalog,
    ContentId,
    DuplicateGroupHandle,
    DuplicateGroupView,
    FileObservation,
    RefreshFailure,
    _sqlite_query,
)
from archiver._query import DuplicateGroupQueryDescription
from archiver.query import SortExpression, duplicate, size


def _copies(root: Path, prefix: str, data: bytes, count: int = 2) -> None:
    directory = root / prefix
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"copy-{index:02}.bin").write_bytes(data)


def _seed(root: Path, count: int = 100) -> list[tuple[ContentId, int, int]]:
    records = []
    for index in range(count):
        data = b"" if index == 0 else bytes([index]) * (index % 4 + 1)
        copies = 2 + index % 3
        _copies(root, f"nested/g{index:03}", data, copies)
        records.append((ContentId("sha256", hashlib.sha256(data).hexdigest()), len(data), copies))
    return records


def _ids(rows: tuple[DuplicateGroupView, ...]) -> list[ContentId]:
    return [row.content_id for row in rows]


@pytest.mark.parametrize(
    ("ordering", "metric", "descending"),
    [
        (size.asc(), "size", False),
        (size.desc(), "size", True),
        (duplicate.file_count.asc(), "count", False),
        (duplicate.file_count.desc(), "count", True),
        (duplicate.redundant_bytes.asc(), "redundant", False),
        (duplicate.redundant_bytes.desc(), "redundant", True),
    ],
)
def test_hundred_groups_page_in_every_supported_order(
    tmp_path: Path,
    ordering: SortExpression,
    metric: str,
    descending: bool,
) -> None:
    root = tmp_path / "root"
    records = _seed(root)

    def key(record: tuple[ContentId, int, int]) -> tuple[int, str, str]:
        content_id, length, count = record
        value = {"size": length, "count": count, "redundant": (count - 1) * length}[metric]
        return (-value if descending else value, content_id.algorithm, content_id.digest)

    expected = [record[0] for record in sorted(records, key=key)]
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        query = catalog.duplicates(root).sort(ordering).limit(10).member_limit(1)
        summary = query.summary()
        for offset in range(0, 100, 10):
            page = query.offset(offset)
            rows = page.fetch()
            assert _ids(rows) == expected[offset : offset + 10]
            assert [handle.content_id for handle in page.stream()] == _ids(rows)
            assert page.summary() == summary
            assert all(len(row.members) == 1 for row in rows)
            for row in rows:
                assert row.potential_redundant_bytes == (row.file_instance_count - 1) * row.size_bytes
        assert _ids(query.offset(97).fetch()) == expected[97:]
        for offset in (100, 101, 2**63 - 1):
            assert query.offset(offset).fetch() == ()
            assert tuple(query.offset(offset).stream_all()) == ()
        suffix = query.offset(90).limit(1)
        assert [handle.content_id for handle in suffix.stream_all()] == expected[90:]
        assert [handle.content_id for handle in suffix.offset(0).stream_all()] == expected
        assert _ids(query.offset(10).limit(5).sort(ordering).fetch()) == expected[10:15]
        assert _ids(query.sort(ordering).limit(5).offset(10).fetch()) == expected[10:15]
        handle = next(iter(suffix.fetch()))
        assert len(handle.members) == 1
        streamed = tuple(suffix.stream())[0]
        assert len(tuple(streamed.stream_members())) == 1
        assert len(tuple(streamed.stream_all_members())) == streamed.file_instance_count


def test_skipped_groups_and_members_are_not_materialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    _seed(root)
    compiled: list[DuplicateGroupQueryDescription] = []
    selected_rows = 0
    members: list[ContentId] = []
    original_rows = _sqlite_query.iter_duplicate_group_rows
    original_compile = _sqlite_query.compile_duplicate_group_members

    def rows(
        connection: sqlite3.Connection, description: DuplicateGroupQueryDescription
    ) -> Generator[sqlite3.Row, None, None]:
        nonlocal selected_rows
        compiled.append(description)
        with closing(original_rows(connection, description)) as cursor:
            for row in cursor:
                selected_rows += 1
                yield row

    def member_query(description: DuplicateGroupQueryDescription, content_id: ContentId) -> _sqlite_query.SqliteQuery:
        members.append(content_id)
        return original_compile(description, content_id)

    monkeypatch.setattr(_sqlite_query, "iter_duplicate_group_rows", rows)
    monkeypatch.setattr(_sqlite_query, "compile_duplicate_group_members", member_query)
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        page = catalog.duplicates(root).offset(90).limit(3).member_limit(1).fetch()
        assert selected_rows == 3
        assert members == _ids(page)
        assert len(compiled) == 1 and compiled[0].group_offset == 90


@pytest.mark.parametrize("change", ["insert", "remove"])
def test_pages_are_live_but_started_streams_and_handles_remain_pinned(tmp_path: Path, change: str) -> None:
    root = tmp_path / "root"
    for letter, length in [("a", 1), ("b", 2), ("c", 3), ("d", 4)]:
        _copies(root, letter, letter.encode() * length)
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        query = catalog.duplicates(root).sort(size.desc()).limit(2)
        old_sequence = _ids(query.limit(10).fetch())
        first_page = _ids(query.fetch())
        delayed = cast(Generator[DuplicateGroupHandle, None, None], query.offset(2).stream())
        with closing(cast(Generator[DuplicateGroupHandle, None, None], query.offset(1).stream_all())) as started:
            retained = next(started)
            assert retained.content_id == old_sequence[1]
            # Change the retained group's current membership as well as an earlier rank.
            (root / "c" / "copy-02.bin").write_bytes(b"ccc")
            if change == "insert":
                _copies(root, "e", b"eeeee")
            else:
                (root / "d" / "copy-01.bin").unlink()
            catalog.scan_directory(root)
            current_sequence = _ids(query.limit(10).fetch())
            second_page = _ids(query.offset(2).fetch())
            assert second_page == current_sequence[2:4]
            with closing(delayed):
                assert [handle.content_id for handle in delayed] == second_page
            assert [handle.content_id for handle in started] == old_sequence[2:]
        with closing(cast(Generator[FileObservation, None, None], retained.stream_all_members())) as members:
            assert len(tuple(members)) == 2
        current_retained = next(row for row in query.limit(10).fetch() if row.content_id == retained.content_id)
        assert current_retained.file_instance_count == 3
        if change == "insert":
            assert first_page[-1] == second_page[0]  # An inserted earlier group repeats a position.
        else:
            assert old_sequence[2] not in first_page + second_page  # Removal can skip a group.


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_refresh_and_read_failures_preserve_paged_state_and_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted: bool,
) -> None:
    root = tmp_path / "root"
    _seed(root, 5)
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog:
        catalog.scan_directory(root)
        query = catalog.duplicates(root).offset(1).limit(2)
        expected = query.fetch()
        _copies(root, "not-published", b"new-leading-group")
        history = tuple(catalog.observation_history())
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in root.rglob("*.bin")}
        data_before = database.read_bytes()

        def fail_progress(_progress: object) -> None:
            if interrupted:
                raise KeyboardInterrupt("controlled interruption")
            raise RuntimeError("controlled failure")

        with pytest.raises(KeyboardInterrupt if interrupted else RefreshFailure):
            catalog.scan_directory(root, progress_callback=fail_progress)
        assert query.fetch() == expected
        assert tuple(catalog.observation_history()) == history
        original = _sqlite_query.compile_duplicate_groups
        with monkeypatch.context() as patch:
            patch.setattr(
                _sqlite_query,
                "compile_duplicate_groups",
                lambda description: _sqlite_query.SqliteQuery("SELECT * FROM missing_page", ()),
            )
            with pytest.raises(sqlite3.OperationalError):
                query.fetch()
            with pytest.raises(sqlite3.OperationalError):
                tuple(query.stream())
        assert _sqlite_query.compile_duplicate_groups is original
        assert database.read_bytes() == data_before
        assert {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in root.rglob("*.bin")} == before
    with Catalog.open(database) as reopened:
        assert reopened.duplicates(root).offset(1).limit(2).fetch() == expected
        assert reopened.schema_version == 2


def test_unknown_unscanned_and_empty_pages_keep_zero_summary(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog:
        for scanned in (False, True):
            if scanned:
                catalog.scan_directory(root)
            query = catalog.duplicates(root).offset(10)
            assert query.fetch() == () and tuple(query.stream()) == () and tuple(query.stream_all()) == ()
            assert query.summary().duplicate_content_group_count == 0
        unknown = tmp_path / "unknown"
        unknown.mkdir()
        assert catalog.duplicates(unknown).offset(1).fetch() == ()
