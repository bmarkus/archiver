from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterator
from pathlib import Path

import pytest

from archiver import Catalog
from archiver._query import CurrentObservationQuery
from archiver._sqlite_query import SqliteQuery


def _source_snapshot(paths: Collection[Path]) -> dict[Path, tuple[bytes, int, int]]:
    return {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) for path in paths}


def test_current_files_uses_query_path_with_deterministically_ordered_domain_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    nested = root / "nested"
    nested.mkdir(parents=True)
    empty = root / "empty"
    nested_duplicate = nested / "a-copy.bin"
    top_duplicate = root / "z-copy.bin"
    empty.touch()
    nested_duplicate.write_bytes(b"same")
    top_duplicate.write_bytes(b"same")
    paths_in_order = (empty, nested_duplicate, top_duplicate)
    source_before = _source_snapshot(paths_in_order)
    database_path = tmp_path / "catalog.sqlite"

    def reversed_regular_files(scan_root: Path, excluded_directories: Collection[Path]) -> Iterator[Path]:
        assert scan_root == root.resolve()
        assert not excluded_directories
        yield from reversed(paths_in_order)

    monkeypatch.setattr("archiver.catalog.regular_files", reversed_regular_files)

    with Catalog.create(database_path) as catalog:
        catalog.scan_directory(root)
        current = list(catalog.files(root).stream_all())

        assert [item.relative_path.as_posix() for item in current] == [
            "empty",
            "nested/a-copy.bin",
            "z-copy.bin",
        ]
        assert current[0].size_bytes == 0
        assert current[1].content_id == current[2].content_id
        assert current[1].relative_path != current[2].relative_path
        assert _source_snapshot(paths_in_order) == source_before

    with Catalog.open(database_path) as reopened:
        assert list(reopened.files(root).stream_all()) == current


def test_current_files_returns_empty_for_unknown_and_not_yet_scanned_locations(tmp_path: Path) -> None:
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
        assert list(catalog.files(known_root).stream_all()) == []
        assert list(catalog.files(unknown_root).stream_all()) == []


def test_current_files_execution_failure_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry.txt"
    entry.write_bytes(b"stable")
    source_before = _source_snapshot((entry,))

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        expected_current = list(catalog.files(root).stream_all())
        expected_history = list(catalog.observation_history())
        expected_scan = catalog.current_scan(root)
        expected_schema = catalog.schema_version
        captured: list[CurrentObservationQuery] = []

        def compile_invalid_query(query: CurrentObservationQuery) -> SqliteQuery:
            captured.append(query)
            return SqliteQuery("SELECT * FROM missing_current_observations", ())

        with monkeypatch.context() as patch:
            patch.setattr("archiver._sqlite_query.compile_current_observations", compile_invalid_query)
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                list(catalog.files(root).stream_all())

        assert len(captured) == 1
        assert captured[0].location.root == root.resolve()
        assert list(catalog.files(root).stream_all()) == expected_current
        assert list(catalog.observation_history()) == expected_history
        assert catalog.current_scan(root) == expected_scan
        assert catalog.schema_version == expected_schema
        assert _source_snapshot((entry,)) == source_before
