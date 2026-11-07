from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import archiver
import archiver.models
import archiver.pandas
from archiver import Catalog, ContentId, DuplicateGroupQuery, ScanFailure
from archiver._query import DuplicateGroupQueryDescription
from archiver._sqlite_query import SqliteQuery
from archiver.cli import main


def _write_group(root: Path, directory: str, names: tuple[str, ...], content: bytes) -> None:
    group_directory = root / directory
    group_directory.mkdir()
    for name in names:
        (group_directory / name).write_bytes(content)


def test_duplicate_query_bounds_orders_and_preserves_complete_totals(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _write_group(root, "large", ("z.bin", "a.bin", "m.bin"), b"large")
    _write_group(root, "alpha", ("two.bin", "one.bin"), b"alpha")
    _write_group(root, "bravo", ("two.bin", "one.bin"), b"bravo")
    _write_group(root, "empty", ("two.bin", "one.bin"), b"")
    (root / "unique.bin").write_bytes(b"unique")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        current_scan = catalog.current_scan(root)
        assert current_scan is not None
        source_before = {
            path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        }

        query = catalog.duplicates(root).limit(3).member_limit(2)
        groups = query.fetch()
        summary = query.summary()

        assert summary.duplicate_content_group_count == 4
        assert summary.duplicate_file_instance_count == 9
        assert summary.potential_redundant_bytes == 20
        assert len(groups) == 3
        assert groups[0].size_bytes == 5
        assert groups[0].file_instance_count == 3
        assert groups[0].potential_redundant_bytes == 10
        assert [member.relative_path.as_posix() for member in groups[0].members] == [
            "large/a.bin",
            "large/m.bin",
        ]
        tied_digests = sorted((hashlib.sha256(b"alpha").hexdigest(), hashlib.sha256(b"bravo").hexdigest()))
        assert [group.content_id.digest for group in groups[1:]] == tied_digests
        empty_group = query.limit(4).fetch()[-1]
        assert empty_group.size_bytes == 0
        assert empty_group.file_instance_count == 2
        assert empty_group.potential_redundant_bytes == 0
        assert catalog.current_scan(root) == current_scan
        source_after = {
            path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        }
        assert source_after == source_before

    with Catalog.open(tmp_path / "catalog.sqlite") as reopened:
        persisted = reopened.duplicates(root).limit(1).member_limit(1)
        persisted_groups = persisted.fetch()
        assert persisted.summary() == summary
        assert persisted_groups[0].content_id == groups[0].content_id
        assert persisted_groups[0].file_instance_count == 3
        assert len(persisted_groups[0].members) == 1


def test_duplicate_query_handles_unknown_unscanned_empty_and_invalid_limits(tmp_path: Path) -> None:
    root = tmp_path / "source"
    unknown = tmp_path / "unknown"
    root.mkdir()
    unknown.mkdir()
    (root / "unique.bin").write_bytes(b"unique")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        query = catalog.duplicates(root)
        assert query.fetch() == ()
        assert query.summary().duplicate_content_group_count == 0
        assert catalog.duplicates(unknown).fetch() == ()
        assert catalog.duplicates(unknown).summary().duplicate_file_instance_count == 0

        catalog.scan_directory(root)
        assert query.fetch() == ()
        assert query.summary().duplicate_file_instance_count == 0

        for invalid_limit in (0, -1):
            with pytest.raises(ValueError, match="at least 1"):
                query.limit(invalid_limit)
            with pytest.raises(ValueError, match="at least 1"):
                query.member_limit(invalid_limit)
        for invalid_type in (True, 1.5, "2"):
            with pytest.raises(TypeError, match="integer"):
                query.limit(invalid_type)  # type: ignore[arg-type]
            with pytest.raises(TypeError, match="integer"):
                query.member_limit(invalid_type)  # type: ignore[arg-type]


def test_duplicate_query_is_lazy_and_keeps_the_latest_successful_state(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        query = catalog.duplicates(root)
        assert query.fetch() == ()

        first.write_bytes(b"same")
        second.write_bytes(b"same")
        catalog.scan_directory(root)
        assert len(query.fetch()) == 1

        second.write_bytes(b"different")

        def fail(_: object) -> None:
            raise RuntimeError("controlled callback failure")

        with pytest.raises(ScanFailure, match="reconciliation failed"):
            catalog.scan_directory(root, progress_callback=fail)
        assert len(query.fetch()) == 1

        def interrupt(_: object) -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            catalog.scan_directory(root, progress_callback=interrupt)
        assert len(query.fetch()) == 1

        catalog.scan_directory(root)
        assert query.fetch() == ()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        query.fetch()


def test_duplicate_query_execution_failure_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"stable")
    second.write_bytes(b"stable")
    source_before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)}

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        query = catalog.duplicates(root)
        expected_history = list(catalog.observation_history())
        expected_scan = catalog.current_scan(root)
        expected_schema = catalog.schema_version
        captured: list[DuplicateGroupQueryDescription] = []

        def compile_invalid_query(internal: DuplicateGroupQueryDescription) -> SqliteQuery:
            captured.append(internal)
            return SqliteQuery("SELECT * FROM missing_duplicate_groups", ())

        with monkeypatch.context() as patcher:
            patcher.setattr("archiver._sqlite_query.compile_duplicate_groups", compile_invalid_query)
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                query.fetch()

        with monkeypatch.context() as patcher:
            patcher.setattr("archiver._sqlite_query.compile_duplicate_summary", compile_invalid_query)
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                query.summary()

        assert len(captured) == 2
        assert len(query.fetch()) == 1
        assert query.summary().duplicate_content_group_count == 1
        assert list(catalog.observation_history()) == expected_history
        assert catalog.current_scan(root) == expected_scan
        assert catalog.schema_version == expected_schema
        assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)} == source_before


def test_duplicate_fetch_pins_one_scan_for_groups_and_members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"old")
    second.write_bytes(b"old")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        old_scan = catalog.current_scan(root)
        assert old_scan is not None
        old_row = catalog.files(root).first()
        assert old_row is not None
        old_content = old_row.content_id

        first.write_bytes(b"new")
        second.write_bytes(b"new")
        catalog.scan_directory(root)
        new_scan = catalog.current_scan(root)
        assert new_scan is not None
        assert new_scan.id != old_scan.id

        current_row = catalog.files(root).first()
        assert current_row is not None
        location = current_row.location
        assert location.id is not None
        catalog._connection.execute(
            "UPDATE locations SET current_scan_id = ? WHERE id = ?",
            (old_scan.id, location.id),
        )

        from archiver import _sqlite_query

        original_compile_members = _sqlite_query.compile_duplicate_group_members
        switched = False

        def switch_current_scan(internal: DuplicateGroupQueryDescription, content_id: ContentId) -> SqliteQuery:
            nonlocal switched
            if not switched:
                catalog._connection.execute(
                    "UPDATE locations SET current_scan_id = ? WHERE id = ?",
                    (new_scan.id, location.id),
                )
                switched = True
            return original_compile_members(internal, content_id)

        monkeypatch.setattr(_sqlite_query, "compile_duplicate_group_members", switch_current_scan)
        groups = catalog.duplicates(root).fetch()

        assert switched
        assert len(groups) == 1
        assert groups[0].content_id == old_content
        assert {member.content_id for member in groups[0].members} == {old_content}
        assert catalog.current_scan(root) == new_scan


def test_bounded_duplicate_fetch_does_not_use_legacy_observation_materialization(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _write_group(root, "many", tuple(f"copy-{index}.bin" for index in range(5)), b"same")

    assert not hasattr(Catalog, "_observations_for_query")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        groups = catalog.duplicates(root).limit(1).member_limit(2).fetch()

    assert len(groups) == 1
    assert groups[0].file_instance_count == 5
    assert len(groups[0].members) == 2


def test_phase_one_duplicate_facade_is_removed() -> None:
    assert not hasattr(Catalog, "duplicate_groups")
    assert not hasattr(Catalog, "search_duplicate_groups")
    assert not hasattr(Catalog, "duplicate_summary")
    assert not hasattr(archiver, "DuplicateGroupSearch")
    assert not hasattr(archiver.models, "DuplicateGroupSearch")
    assert not hasattr(archiver.pandas, "duplicate_groups_frame")
    assert DuplicateGroupQuery.__name__ == "DuplicateGroupQuery"


def test_duplicate_cli_constructs_the_public_query_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    _write_group(root, "copies", ("two.bin", "one.bin"), b"same")
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "refresh", str(root)]) == 0
    capsys.readouterr()
    calls: list[tuple[str, int, int]] = []
    original_summary = DuplicateGroupQuery.summary
    original_fetch = DuplicateGroupQuery.fetch

    def capture_summary(query: DuplicateGroupQuery) -> object:
        calls.append(("summary", query.group_limit, query.member_row_limit))
        return original_summary(query)

    def capture_fetch(query: DuplicateGroupQuery) -> object:
        calls.append(("fetch", query.group_limit, query.member_row_limit))
        return original_fetch(query)

    monkeypatch.setattr(DuplicateGroupQuery, "summary", capture_summary)
    monkeypatch.setattr(DuplicateGroupQuery, "fetch", capture_fetch)

    assert (
        main(
            [
                "catalog",
                "duplicates",
                str(root),
                "--details",
                "--group-limit",
                "4",
                "--member-limit",
                "3",
            ]
        )
        == 0
    )
    assert calls == [("summary", 4, 3), ("fetch", 4, 3)]


def test_catalog_duplicates_details_are_bounded_and_aggregate_output_is_compatible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    _write_group(root, "large", ("z.bin", "a.bin", "m.bin"), b"large")
    _write_group(root, "empty", ("two.bin", "one.bin"), b"")

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "refresh", str(root)]) == 0
    capsys.readouterr()

    assert main(["catalog", "duplicates", str(root)]) == 0
    aggregate = capsys.readouterr().out
    assert "Duplicate groups: 2" in aggregate
    assert "Duplicate file instances: 5" in aggregate
    assert "Potential redundant bytes: 10" in aggregate
    assert "Group 1" not in aggregate

    assert (
        main(
            [
                "catalog",
                "duplicates",
                str(root),
                "--details",
                "--group-limit",
                "1",
                "--member-limit",
                "1",
            ]
        )
        == 0
    )
    details = capsys.readouterr().out
    digest = hashlib.sha256(b"large").hexdigest()
    assert "GROUP  SHA-256        COPIES  SIZE   POTENTIAL REDUNDANT  PATHS" in details
    assert f"1      {digest[:12]}…" in details
    assert "3       5 B    10 B" in details
    assert "1 of 3" in details
    assert "\n\t       large/a.bin\n" in details
    assert "large/m.bin" not in details
    assert "... showing first 1 of 3 paths" not in details
    assert "Showing first 1 of 2 duplicate groups." in details


def test_catalog_duplicates_details_report_no_current_or_duplicate_groups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    (root / "unique.bin").write_bytes(b"unique")
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()

    assert main(["catalog", "duplicates", str(root), "--details"]) == 0
    assert "Current scan: none" in capsys.readouterr().out

    assert main(["catalog", "refresh", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "duplicates", str(root), "--details"]) == 0
    output = capsys.readouterr().out
    assert "Duplicate groups: 0" in output
    assert "No duplicate groups." in output
