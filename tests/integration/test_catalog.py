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


def test_observation_history_retains_complete_context_across_scans(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    renamed = source / "old-name.txt"
    removed = source / "removed.txt"
    changed = source / "changed.txt"
    renamed.write_bytes(b"unchanged")
    removed.write_bytes(b"removed")
    changed.write_bytes(b"first")
    first_changed_content = hash_file(changed)
    first_changed_mtime = changed.stat().st_mtime_ns
    catalog = Catalog.create(tmp_path / "catalog.sqlite")

    catalog.scan_directory(source)
    renamed.rename(source / "new-name.txt")
    removed.unlink()
    changed.write_bytes(b"second")
    second_changed_content = hash_file(changed)
    second_changed_mtime = changed.stat().st_mtime_ns
    catalog.scan_directory(source)

    scans = list(catalog.scan_history())
    history = list(catalog.observation_history())

    assert [scan.status for scan in scans] == ["completed", "completed"]
    assert [(observation.scan.id, observation.relative_path.as_posix()) for observation in history] == [
        (scans[0].id, "changed.txt"),
        (scans[0].id, "old-name.txt"),
        (scans[0].id, "removed.txt"),
        (scans[1].id, "changed.txt"),
        (scans[1].id, "new-name.txt"),
    ]
    first_changed = history[0]
    second_changed = history[3]
    assert first_changed.scan.location.root == source.resolve()
    assert first_changed.content_id == first_changed_content
    assert first_changed.size_bytes == len(b"first")
    assert first_changed.mtime_ns == first_changed_mtime
    assert second_changed.content_id == second_changed_content
    assert second_changed.size_bytes == len(b"second")
    assert second_changed.mtime_ns == second_changed_mtime


def test_history_is_catalog_wide_and_persists_after_reopen(tmp_path: Path) -> None:
    first_root = tmp_path / "a-root"
    second_root = tmp_path / "b-root"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "first.txt").write_bytes(b"first")
    (second_root / "second.txt").write_bytes(b"second")
    database_path = tmp_path / "catalog.sqlite"
    catalog = Catalog.create(database_path)

    catalog.scan_directory(second_root)
    catalog.scan_directory(first_root)
    expected_scans = list(catalog.scan_history())
    expected_observations = list(catalog.observation_history())
    catalog.close()

    with Catalog.open(database_path) as reopened:
        assert list(reopened.scan_history()) == expected_scans
        assert list(reopened.observation_history()) == expected_observations

    assert [scan.location.root for scan in expected_scans] == [first_root.resolve(), second_root.resolve()]
    assert [observation.scan.location.root for observation in expected_observations] == [
        first_root.resolve(),
        second_root.resolve(),
    ]


def test_history_includes_empty_and_failed_scans_without_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_root = tmp_path / "empty"
    source = tmp_path / "source"
    empty_root.mkdir()
    source.mkdir()
    file_path = source / "entry.txt"
    file_path.write_bytes(b"first")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")

    catalog.scan_directory(empty_root)
    catalog.scan_directory(source)
    expected_current = catalog.current_files(source)
    file_path.write_bytes(b"second")

    def fail_hashing(path: Path) -> ContentId:
        raise OSError("controlled hashing failure")

    monkeypatch.setattr("archiver.catalog.hash_file", fail_hashing)
    with pytest.raises(ScanFailure, match="scan failed"):
        catalog.scan_directory(source)

    scans = list(catalog.scan_history())
    history = list(catalog.observation_history())

    assert [scan.status for scan in scans] == ["completed", "completed", "failed"]
    assert scans[0].location.root == empty_root.resolve()
    assert scans[0].completed_at_ns is not None
    assert scans[2].completed_at_ns is None
    assert [observation.relative_path.as_posix() for observation in history] == ["entry.txt"]
    assert catalog.current_files(source) == expected_current


def test_observation_history_is_lazy_and_current_queries_exclude_history_only_rows(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    first = source / "first.txt"
    second = source / "second.txt"
    first.write_bytes(b"duplicate")
    second.write_bytes(b"duplicate")
    catalog = Catalog.create(tmp_path / "catalog.sqlite")
    catalog.scan_directory(source)
    historical_content = hash_file(first)
    history = catalog.observation_history()

    assert iter(history) is history
    assert next(history).content_id == historical_content

    first.write_bytes(b"current first")
    second.write_bytes(b"current second")
    catalog.scan_directory(source)

    assert catalog.find_by_content(source, historical_content) == []
    assert catalog.duplicate_groups(source) == []
    assert [item.relative_path.as_posix() for item in catalog.current_files(source)] == ["first.txt", "second.txt"]
