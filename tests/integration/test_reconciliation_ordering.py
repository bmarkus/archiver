from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, ContentId
from archiver.cli import main
from archiver.hashing import hash_file_stably


def _populate(root: Path) -> dict[str, bytes]:
    entries = {
        "bundle.zip": b"zip",
        "bundle/leaf.txt": b"leaf",
        "bundle/sub.zip": b"nested zip",
        "bundle/sub/file": b"nested file",
        "bundle-1": b"punctuation",
        "bundle space.zip": b"space zip",
        "bundle space/item": b"space item",
        "תיקיה.zip": b"unicode zip",
        "תיקיה/empty": b"",
    }
    for name, data in entries.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return entries


def _source_snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _database_snapshot(database: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.iterdump())


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_prefix_paths_refresh_once_each_across_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verify_hashes: bool
) -> None:
    root = tmp_path / "source"
    entries = _populate(root)
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog:
        catalog.scan_directory(root)
        # The connection created by create() must also use the correct ordering.
        initial_repeat = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        assert initial_repeat.summary.unchanged_files == len(entries)
        assert initial_repeat.summary.new_files == initial_repeat.summary.missing_files == 0

    hashed: list[Path] = []

    def record_hash(path: Path) -> tuple[ContentId, int, int]:
        hashed.append(path)
        return hash_file_stably(path)

    monkeypatch.setattr("archiver.catalog.hash_file_stably", record_hash)
    with Catalog.open(database) as catalog:
        history = tuple(catalog.observation_history())
        source_before = _source_snapshot(root)
        database_before = _database_snapshot(database)
        unchanged = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        assert _database_snapshot(database) == database_before
        assert unchanged.summary.unchanged_files == len(entries)
        assert unchanged.summary.new_files == unchanged.summary.missing_files == unchanged.summary.modified_files == 0
        assert len(hashed) == (len(entries) if verify_hashes else 0)
        assert set(hashed) == ({root / name for name in entries} if verify_hashes else set())
        assert all(change.hash_reused is not verify_hashes for change in unchanged.changes)
        assert [change.relative_path.as_posix() for change in unchanged.changes] == sorted(
            entries, key=lambda name: PurePosixPath(name).parts
        )
        # Public query order is intentionally unchanged (complete POSIX strings).
        assert [item.relative_path.as_posix() for item in catalog.files(root).stream_all()] == sorted(entries)
        assert _source_snapshot(root) == source_before

        changed_path = root / "bundle/sub/file"
        changed_path.write_bytes(b"changed content")
        hashed.clear()
        source_before = _source_snapshot(root)
        changed = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        assert changed.summary.modified_files == 1
        assert changed.summary.unchanged_files == len(entries) - 1
        assert changed.summary.new_files == changed.summary.missing_files == 0
        assert [c.relative_path.as_posix() for c in changed.changes if c.kind == "modified"] == ["bundle/sub/file"]
        assert len(hashed) == (len(entries) if verify_hashes else 1)
        assert _database_snapshot(database) == database_before
        summary = catalog.apply_refresh(changed)
        assert summary.files_observed == len(entries)
        assert _source_snapshot(root) == source_before
        current = catalog.files(root).fetch()
        assert all(row in tuple(catalog.observation_history()) for row in history)
    with Catalog.open(database) as catalog:
        assert catalog.files(root).fetch() == current
        assert catalog.reconcile_directory(root, verify_hashes=verify_hashes).summary.unchanged_files == len(entries)


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_real_changes_and_file_directory_replacements(tmp_path: Path, verify_hashes: bool) -> None:
    root = tmp_path / "source"
    _populate(root)
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        (root / "bundle.zip").unlink()
        (root / "bundle/new").write_bytes(b"new")
        # Replace a file with a directory containing a file, and the reverse.
        (root / "bundle-1").unlink()
        (root / "bundle-1").mkdir()
        (root / "bundle-1/child").write_bytes(b"child")
        (root / "תיקיה/empty").unlink()
        (root / "תיקיה").rmdir()
        (root / "תיקיה").write_bytes(b"now a file")
        source_before = _source_snapshot(root)
        changes = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        assert {(c.relative_path.as_posix(), c.kind) for c in changes.changes if c.kind != "unchanged"} == {
            ("bundle.zip", "missing"),
            ("bundle/new", "new"),
            ("bundle-1", "missing"),
            ("bundle-1/child", "new"),
            ("תיקיה/empty", "missing"),
            ("תיקיה", "new"),
        }
        assert len({c.relative_path for c in changes.changes}) == len(changes.changes)
        catalog.apply_refresh(changes)
        assert {item.relative_path.as_posix() for item in catalog.files(root).stream_all()} == set(source_before)
        assert _source_snapshot(root) == source_before


@pytest.mark.parametrize("command", ["refresh", "scan"])
@pytest.mark.parametrize("verify_hashes", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_prefix_path_changes_are_accurate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, verify_hashes: bool, dry_run: bool
) -> None:
    root = tmp_path / "source"
    entries = _populate(root)
    assert main(["catalog", "init", str(root)]) == 0
    assert main(["catalog", "refresh", str(root)]) == 0
    database = root / ".archiver/catalog.sqlite"
    database_before = _database_snapshot(database)
    entry = root / "bundle/sub/file"
    metadata = entry.stat()
    entry.write_bytes(b"changed")
    os.utime(entry, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
    capsys.readouterr()
    arguments = ["catalog", command, str(root), "--details", "changes", "--no-progress"]
    if verify_hashes:
        arguments.append("--verify-hashes")
    if dry_run:
        arguments.append("--dry-run")
    assert main(arguments) == 0
    output = capsys.readouterr()
    assert "New files: 0\n" in output.out
    assert "Missing files: 0\n" in output.out
    assert "Modified files: 1\n" in output.out
    assert f"Unchanged files: {len(entries) - 1}\n" in output.out
    assert "MODIFIED  bundle/sub/file\n" in output.out
    assert "NEW       " not in output.out and "MISSING   " not in output.out
    assert output.err == ""
    if dry_run:
        assert _database_snapshot(database) == database_before
    with Catalog.open(database) as catalog:
        summary = catalog.current_summary(root)
        assert summary is not None and summary.files_observed == len(entries)
