from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from archiver import Catalog
from archiver.cli import main


def _write_group(root: Path, directory: str, names: tuple[str, ...], content: bytes) -> None:
    group_directory = root / directory
    group_directory.mkdir()
    for name in names:
        (group_directory / name).write_bytes(content)


def test_search_duplicate_groups_bounds_orders_and_preserves_complete_totals(tmp_path: Path) -> None:
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

        result = catalog.search_duplicate_groups(root, group_limit=3, member_limit=2)

        assert result.summary.duplicate_content_group_count == 4
        assert result.summary.duplicate_file_instance_count == 9
        assert result.summary.potential_redundant_bytes == 20
        assert len(result.groups) == 3
        assert result.groups[0].size_bytes == 5
        assert result.groups[0].file_instance_count == 3
        assert result.groups[0].potential_redundant_bytes == 10
        assert [member.relative_path.as_posix() for member in result.groups[0].members] == [
            "large/a.bin",
            "large/m.bin",
        ]
        tied_digests = sorted((hashlib.sha256(b"alpha").hexdigest(), hashlib.sha256(b"bravo").hexdigest()))
        assert [group.content_id.digest for group in result.groups[1:]] == tied_digests
        assert len(catalog.duplicate_groups(root)) == 4
        assert catalog.current_scan(root) == current_scan
        source_after = {
            path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        }
        assert source_after == source_before

    with Catalog.open(tmp_path / "catalog.sqlite") as reopened:
        persisted = reopened.search_duplicate_groups(root, group_limit=1, member_limit=1)
        assert persisted.summary == result.summary
        assert persisted.groups[0].content_id == result.groups[0].content_id
        assert persisted.groups[0].file_instance_count == 3
        assert len(persisted.groups[0].members) == 1


def test_search_duplicate_groups_handles_unscanned_empty_and_invalid_limits(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "unique.bin").write_bytes(b"unique")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        unscanned = catalog.search_duplicate_groups(root)
        assert unscanned.groups == ()
        assert unscanned.summary.duplicate_content_group_count == 0

        catalog.scan_directory(root)
        scanned = catalog.search_duplicate_groups(root)
        assert scanned.groups == ()
        assert scanned.summary.duplicate_file_instance_count == 0

        with pytest.raises(ValueError, match="group_limit"):
            catalog.search_duplicate_groups(root, group_limit=0)
        with pytest.raises(ValueError, match="member_limit"):
            catalog.search_duplicate_groups(root, member_limit=0)


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
    assert "       large/a.bin" in details
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
