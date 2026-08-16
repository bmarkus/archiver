import sqlite3
from pathlib import Path

import pytest

from archiver import Catalog, ContentId, InvalidCatalogError, ScanFailure
from archiver.hashing import hash_file


def test_create_close_and_reopen_preserves_catalog_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite"
    catalog = Catalog.create(database_path)
    catalog_uuid = catalog.catalog_uuid
    catalog.close()

    reopened = Catalog.open(database_path)
    assert reopened.catalog_uuid == catalog_uuid
    assert reopened.schema_version == 1
    reopened.close()


def test_unsupported_schema_version_fails_clearly(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite"
    Catalog.create(database_path).close()
    connection = sqlite3.connect(database_path)
    connection.execute("UPDATE catalog_metadata SET schema_version = 999")
    connection.commit()
    connection.close()

    with pytest.raises(InvalidCatalogError, match="unsupported catalog schema version"):
        Catalog.open(database_path)


def test_scan_single_file_and_keep_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file_path = source / "entry.txt"
    before = b"catalog me"
    file_path.write_bytes(before)
    catalog = Catalog.create(tmp_path / "catalog.sqlite")

    summary = catalog.scan_directory(source)
    files = catalog.current_files(source)

    assert summary.files_observed == 1
    assert summary.total_bytes_observed == len(before)
    assert summary.distinct_content_count == 1
    assert summary.duplicate_content_group_count == 0
    assert [entry.relative_path.as_posix() for entry in files] == ["entry.txt"]
    assert files[0].content_id == hash_file(file_path)
    assert files[0].size_bytes == len(before)
    assert file_path.read_bytes() == before


def test_scan_nested_paths_are_relative_and_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "b").mkdir(parents=True)
    (source / "a.txt").write_bytes(b"a")
    (source / "b" / "c.txt").write_bytes(b"c")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")

    catalog.scan_directory(source)

    assert [item.relative_path.as_posix() for item in catalog.current_files(source)] == [
        "a.txt",
        "b/c.txt",
    ]


def test_duplicate_content_is_grouped_by_content_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_bytes(b"same")
    (source / "two.txt").write_bytes(b"same")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")

    summary = catalog.scan_directory(source)
    groups = catalog.duplicate_groups(source)

    assert summary.distinct_content_count == 1
    assert summary.duplicate_content_group_count == 1
    assert [[entry.relative_path.as_posix() for entry in group] for group in groups] == [["one.txt", "two.txt"]]


def test_same_name_with_different_bytes_is_not_duplicate(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "same-name.txt").write_bytes(b"first")
    (second_root / "same-name.txt").write_bytes(b"second")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")

    catalog.scan_directory(first_root)
    catalog.scan_directory(second_root)

    assert catalog.duplicate_groups(first_root) == []
    assert catalog.duplicate_groups(second_root) == []


def test_rename_preserves_content_identity_between_scans(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = source / "old.txt"
    original.write_bytes(b"unchanged")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")
    catalog.scan_directory(source)
    content_id = catalog.current_files(source)[0].content_id
    original.rename(source / "new.txt")

    catalog.scan_directory(source)

    current = catalog.current_files(source)
    assert current[0].relative_path.as_posix() == "new.txt"
    assert current[0].content_id == content_id


def test_deletion_disappears_from_current_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    retained = source / "retained.txt"
    removed = source / "removed.txt"
    retained.write_bytes(b"keep")
    removed.write_bytes(b"remove")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")
    catalog.scan_directory(source)
    removed.unlink()

    catalog.scan_directory(source)

    assert [item.relative_path.as_posix() for item in catalog.current_files(source)] == ["retained.txt"]


def test_changed_path_points_to_new_content_after_a_later_scan(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file_path = source / "entry.txt"
    file_path.write_bytes(b"first")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")
    catalog.scan_directory(source)
    first_content = catalog.current_files(source)[0].content_id
    file_path.write_bytes(b"second")

    catalog.scan_directory(source)

    assert catalog.current_files(source)[0].content_id != first_content


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target.txt"
    target.write_bytes(b"outside")
    link = source / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not available on this platform")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")

    catalog.scan_directory(source)

    assert catalog.current_files(source) == []


def test_failed_scan_preserves_previous_current_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file_path = source / "entry.txt"
    file_path.write_bytes(b"first")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")
    catalog.scan_directory(source)
    expected = catalog.current_files(source)
    file_path.write_bytes(b"second")

    def fail_hashing(path: Path) -> ContentId:
        raise OSError("controlled hashing failure")

    monkeypatch.setattr("archiver.catalog.hash_file", fail_hashing)
    with pytest.raises(ScanFailure, match="scan failed"):
        catalog.scan_directory(source)

    assert catalog.current_files(source) == expected


def test_catalog_database_and_sidecars_are_excluded_when_inside_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.txt").write_bytes(b"source data")
    database_path = source / "catalog.sqlite"
    catalog = Catalog.create(database_path)

    catalog.scan_directory(source)

    assert [item.relative_path.as_posix() for item in catalog.current_files(source)] == ["entry.txt"]
