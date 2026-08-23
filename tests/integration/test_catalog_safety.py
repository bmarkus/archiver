from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from archiver import Catalog, InvalidCatalogError


def test_directory_symlinks_are_not_traversed(tmp_path: Path) -> None:
    root = tmp_path / "source"
    inside = root / "inside"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    inside_file = inside / "visible.txt"
    inside_file.write_bytes(b"inside")
    (outside / "external.txt").write_bytes(b"outside")
    try:
        (root / "inside-link").symlink_to(inside, target_is_directory=True)
        (root / "outside-link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this platform")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)

        assert [item.relative_path.as_posix() for item in catalog.current_files(root)] == ["inside/visible.txt"]
    assert inside_file.read_bytes() == b"inside"


def test_locations_with_one_shared_content_instance_remain_isolated(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_file = first_root / "shared.bin"
    second_file = second_root / "shared.bin"
    first_file.write_bytes(b"shared")
    second_file.write_bytes(b"shared")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(first_root)
        catalog.scan_directory(second_root)
        first_scan = catalog.current_scan(first_root)
        second_scan = catalog.current_scan(second_root)
        second_current = catalog.current_files(second_root)

        assert first_scan is not None and second_scan is not None
        assert first_scan.id != second_scan.id
        assert catalog.duplicate_groups(first_root) == []
        assert catalog.duplicate_groups(second_root) == []
        assert catalog.search_duplicate_groups(first_root).summary.duplicate_content_group_count == 0
        assert catalog.search_duplicate_groups(second_root).summary.duplicate_content_group_count == 0

        first_file.write_bytes(b"changed")
        catalog.scan_directory(first_root)

        assert catalog.current_files(second_root) == second_current
        assert catalog.current_scan(second_root) == second_scan
        assert catalog.duplicate_groups(second_root) == []


def test_open_rejects_invalid_catalogs_and_create_refuses_existing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(InvalidCatalogError, match="does not exist"):
        Catalog.open(missing)

    empty_database = tmp_path / "empty.sqlite"
    sqlite3.connect(empty_database).close()
    with pytest.raises(InvalidCatalogError, match="could not open catalog"):
        Catalog.open(empty_database)

    non_sqlite = tmp_path / "not-a-database.sqlite"
    non_sqlite.write_bytes(b"not sqlite")
    with pytest.raises(InvalidCatalogError, match="could not open catalog"):
        Catalog.open(non_sqlite)

    missing_metadata = tmp_path / "missing-metadata.sqlite"
    Catalog.create(missing_metadata).close()
    connection = sqlite3.connect(missing_metadata)
    connection.execute("DELETE FROM catalog_metadata")
    connection.commit()
    connection.close()
    with pytest.raises(InvalidCatalogError, match="metadata is missing"):
        Catalog.open(missing_metadata)

    malformed_version = tmp_path / "malformed-version.sqlite"
    Catalog.create(malformed_version).close()
    connection = sqlite3.connect(malformed_version)
    connection.execute("UPDATE catalog_metadata SET schema_version = 'invalid'")
    connection.commit()
    connection.close()
    with pytest.raises(InvalidCatalogError, match="unsupported catalog schema version"):
        Catalog.open(malformed_version)

    occupied = tmp_path / "occupied.sqlite"
    occupied.write_bytes(b"do not overwrite")
    with pytest.raises(InvalidCatalogError, match="already exists"):
        Catalog.create(occupied)
    assert occupied.read_bytes() == b"do not overwrite"


def test_running_and_failed_history_rows_never_become_current_state(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "entry.txt").write_bytes(b"stable")
    database_path = tmp_path / "catalog.sqlite"

    with Catalog.create(database_path) as catalog:
        catalog.scan_directory(root)
        expected_current = catalog.current_files(root)
        expected_scan = catalog.current_scan(root)
        expected_summary = catalog.current_summary(root)

    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    location_id = connection.execute("SELECT id FROM locations").fetchone()
    content_id = connection.execute("SELECT id FROM content").fetchone()
    assert location_id is not None and content_id is not None
    running = connection.execute(
        "INSERT INTO scan_runs (location_id, started_at_ns, status) VALUES (?, ?, 'running')",
        (location_id[0], 10),
    )
    failed = connection.execute(
        "INSERT INTO scan_runs (location_id, started_at_ns, completed_at_ns, status) VALUES (?, ?, ?, 'failed')",
        (location_id[0], 20, 30),
    )
    assert running.lastrowid is not None and failed.lastrowid is not None
    connection.execute(
        (
            "INSERT INTO file_observations "
            "(scan_id, relative_path, content_id, size_bytes, mtime_ns) "
            "VALUES (?, ?, ?, ?, ?)"
        ),
        (running.lastrowid, "running.txt", content_id[0], len(b"stable"), 1),
    )
    connection.execute(
        (
            "INSERT INTO file_observations "
            "(scan_id, relative_path, content_id, size_bytes, mtime_ns) "
            "VALUES (?, ?, ?, ?, ?)"
        ),
        (failed.lastrowid, "failed.txt", content_id[0], len(b"stable"), 2),
    )
    connection.commit()
    connection.close()

    with Catalog.open(database_path) as catalog:
        assert [scan.status for scan in catalog.scan_history()] == ["completed", "running", "failed"]
        assert [item.relative_path.as_posix() for item in catalog.observation_history()] == [
            "entry.txt",
            "running.txt",
            "failed.txt",
        ]
        assert catalog.current_scan(root) == expected_scan
        assert catalog.current_summary(root) == expected_summary
        assert catalog.current_files(root) == expected_current
        assert catalog.duplicate_groups(root) == []
