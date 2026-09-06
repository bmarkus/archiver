from __future__ import annotations

import os
import sqlite3
from collections.abc import Collection
from pathlib import Path

import pytest

from archiver import Catalog, CurrentFileQuery, ScanFailure
from archiver._query import CurrentObservationQuery
from archiver._sqlite_query import SqliteQuery
from archiver.query import mtime, path, size


def _write_file(file_path: Path, contents: bytes, mtime_ns: int) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(contents)
    os.utime(file_path, ns=(mtime_ns, mtime_ns))


def _paths(rows: Collection[object]) -> list[str]:
    return [row.relative_path.as_posix() for row in rows]  # type: ignore[attr-defined]


def _source_snapshot(paths: Collection[Path]) -> dict[Path, tuple[bytes, int, int]]:
    return {item: (item.read_bytes(), item.stat().st_mtime_ns, item.stat().st_mode) for item in paths}


def test_files_query_defaults_to_path_order_and_twenty_row_bound(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    for index in reversed(range(25)):
        (root / f"item-{index:02}.txt").write_text(str(index), encoding="utf-8")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        query = catalog.files(root)
        rows = query.fetch()

    assert isinstance(query, CurrentFileQuery)
    assert isinstance(rows, tuple)
    assert _paths(rows) == [f"item-{index:02}.txt" for index in range(20)]


def test_files_query_filters_orders_replaces_and_ignores_method_order(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _write_file(root / "a-small.txt", b"a", 100)
    _write_file(root / "b-tie.txt", b"bb", 200)
    _write_file(root / "c-tie.txt", b"cc", 200)
    _write_file(root / "nested" / "recent.txt", b"ddd", 300)
    _write_file(root / "nested" / "other.bin", b"xxxx", 400)

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        base = catalog.files(root).limit(10)

        assert _paths(base.sort(path.asc()).fetch()) == [
            "a-small.txt",
            "b-tie.txt",
            "c-tie.txt",
            "nested/other.bin",
            "nested/recent.txt",
        ]
        assert _paths(base.sort(path.desc()).fetch()) == [
            "nested/recent.txt",
            "nested/other.bin",
            "c-tie.txt",
            "b-tie.txt",
            "a-small.txt",
        ]
        assert _paths(base.sort(size.asc()).fetch()) == [
            "a-small.txt",
            "b-tie.txt",
            "c-tie.txt",
            "nested/recent.txt",
            "nested/other.bin",
        ]
        assert _paths(base.sort(size.desc()).fetch()) == [
            "nested/other.bin",
            "nested/recent.txt",
            "b-tie.txt",
            "c-tie.txt",
            "a-small.txt",
        ]
        assert _paths(base.sort(mtime.asc()).fetch()) == [
            "a-small.txt",
            "b-tie.txt",
            "c-tie.txt",
            "nested/recent.txt",
            "nested/other.bin",
        ]
        assert _paths(base.sort(mtime.desc()).fetch()) == [
            "nested/other.bin",
            "nested/recent.txt",
            "b-tie.txt",
            "c-tie.txt",
            "a-small.txt",
        ]

        filtered = base.where(path.glob("nested/*")).where(path.glob("*.txt"))
        assert _paths(filtered.fetch()) == ["nested/recent.txt"]

        replaced = base.sort(size.desc()).sort(path.desc()).limit(1).limit(3)
        assert _paths(replaced.fetch()) == ["nested/recent.txt", "nested/other.bin", "c-tie.txt"]

        first_order = base.where(path.glob("*.txt")).sort(size.desc()).limit(2)
        second_order = base.limit(2).sort(size.desc()).where(path.glob("*.txt"))
        assert first_order == second_order
        assert first_order.fetch() == second_order.fetch()
        assert base.predicates == ()
        assert base.row_limit == 10


def test_query_is_lazy_reusable_and_tracks_latest_successful_scan(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    original = root / "original.txt"
    query_database = tmp_path / "catalog.sqlite"

    with Catalog.create(query_database) as catalog:
        query = catalog.files(root).limit(10)
        assert query.fetch() == ()

        original.write_bytes(b"first")
        catalog.scan_directory(root)
        first_rows = query.fetch()
        assert _paths(first_rows) == ["original.txt"]
        assert query.fetch() == first_rows

        original.unlink()
        replacement = root / "replacement.txt"
        replacement.write_bytes(b"second")
        catalog.scan_directory(root)
        assert _paths(query.fetch()) == ["replacement.txt"]

        replacement.write_bytes(b"changed")

        def fail(_: object) -> None:
            raise RuntimeError("controlled callback failure")

        with pytest.raises(ScanFailure, match="reconciliation failed"):
            catalog.scan_directory(root, progress_callback=fail)
        assert _paths(query.fetch()) == ["replacement.txt"]

        def interrupt(_: object) -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            catalog.scan_directory(root, progress_callback=interrupt)
        assert _paths(query.fetch()) == ["replacement.txt"]

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        query.fetch()

    query_built_after_close = catalog.files(root)
    assert query_built_after_close.root == root.resolve()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        query_built_after_close.fetch()


def test_files_query_returns_empty_for_unknown_and_unscanned_locations(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite"
    known_root = tmp_path / "known"
    unknown_root = tmp_path / "unknown"
    known_root.mkdir()
    unknown_root.mkdir()
    Catalog.create(database_path).close()

    connection = sqlite3.connect(database_path)
    connection.execute("INSERT INTO locations (root_path) VALUES (?)", (str(known_root.resolve()),))
    connection.commit()
    connection.close()

    with Catalog.open(database_path) as catalog:
        assert catalog.files(known_root).fetch() == ()
        assert catalog.files(unknown_root).fetch() == ()


def test_legacy_search_rows_are_selected_by_equivalent_public_queries(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _write_file(root / "a.txt", b"a", 100)
    _write_file(root / "b.txt", b"bb", 200)
    _write_file(root / "nested" / "c.txt", b"ccc", 300)

    cases = (
        (None, "path", False, path.asc()),
        (None, "path", True, path.desc()),
        (None, "size", False, size.desc()),
        (None, "size", True, size.asc()),
        (None, "date", False, mtime.desc()),
        (None, "date", True, mtime.asc()),
        ("nested/*.txt", "path", False, path.asc()),
    )

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        for path_glob, sort_by, reverse, ordering in cases:
            legacy = catalog.search_current_files(
                root,
                path_glob=path_glob,
                sort_by=sort_by,  # type: ignore[arg-type]
                reverse=reverse,
                limit=2,
            )
            direct = catalog.files(root).sort(ordering).limit(2)
            if path_glob is not None:
                direct = direct.where(path.glob(path_glob))
            assert legacy.files == direct.fetch()


def test_search_current_files_delegates_row_selection_to_query_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")
    captured: list[CurrentFileQuery] = []
    original_fetch = CurrentFileQuery.fetch

    def capture_fetch(query: CurrentFileQuery) -> tuple[object, ...]:
        captured.append(query)
        return original_fetch(query)

    monkeypatch.setattr(CurrentFileQuery, "fetch", capture_fetch)
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        result = catalog.search_current_files(root, path_glob="*.txt", sort_by="size", reverse=True, limit=1)

    assert _paths(result.files) == ["a.txt"]
    assert len(captured) == 1
    assert captured[0].predicates == (path.glob("*.txt"),)
    assert captured[0].ordering == size.asc()
    assert captured[0].row_limit == 1


def test_public_query_execution_failure_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry.txt"
    entry.write_bytes(b"stable")
    source_before = _source_snapshot((entry,))

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        query = catalog.files(root)
        expected_rows = query.fetch()
        expected_history = list(catalog.observation_history())
        expected_scan = catalog.current_scan(root)
        expected_schema = catalog.schema_version
        captured: list[CurrentObservationQuery] = []

        def compile_invalid_query(internal: CurrentObservationQuery) -> SqliteQuery:
            captured.append(internal)
            return SqliteQuery("SELECT * FROM missing_current_observations", ())

        with monkeypatch.context() as patcher:
            patcher.setattr("archiver._sqlite_query.compile_current_observations", compile_invalid_query)
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                query.fetch()

        assert len(captured) == 1
        assert query.fetch() == expected_rows
        assert list(catalog.observation_history()) == expected_history
        assert catalog.current_scan(root) == expected_scan
        assert catalog.schema_version == expected_schema
        assert _source_snapshot((entry,)) == source_before
