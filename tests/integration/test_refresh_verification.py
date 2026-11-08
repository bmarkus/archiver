from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest

from archiver import Catalog, ContentId, RefreshFailure, ScanProgress
from archiver.cli import main
from archiver.hashing import FileChangedDuringHashingError, hash_file_stably


def _database_snapshot(database: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.iterdump())


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_refresh_hash_calls_progress_exclusions_and_empty_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verify_hashes: bool
) -> None:
    root = tmp_path / "source"
    (root / "nested").mkdir(parents=True)
    excluded = root / "excluded"
    excluded.mkdir()
    (excluded / "hidden").write_bytes(b"hidden")
    files = [root / "empty", root / "nested" / "copy", root / "original"]
    for path, data in zip(files, [b"", b"same", b"same"], strict=True):
        path.write_bytes(data)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    hashed: list[Path] = []

    def record_hash(path: Path) -> tuple[ContentId, int, int]:
        hashed.append(path)
        return hash_file_stably(path)

    monkeypatch.setattr("archiver.catalog.hash_file_stably", record_hash)
    with Catalog.create(root / "catalog.sqlite") as catalog:
        database_before = _database_snapshot(catalog.database_path)
        progress: list[ScanProgress] = []
        initial = catalog.reconcile_directory(
            root, verify_hashes=verify_hashes, excluded_directories=(excluded,), progress_callback=progress.append
        )
        assert set(hashed) == set(files)
        assert len(hashed) == 3
        assert catalog.current_scan(root) is None
        assert list(catalog.scan_history()) == []
        assert _database_snapshot(catalog.database_path) == database_before
        catalog.apply_refresh(initial)
        hashed.clear()
        progress.clear()
        database_before = _database_snapshot(catalog.database_path)
        changes = catalog.reconcile_directory(
            root, verify_hashes=verify_hashes, excluded_directories=(excluded,), progress_callback=progress.append
        )
        assert set(hashed) == (set(files) if verify_hashes else set())
        assert len(hashed) == (3 if verify_hashes else 0)
        assert all(change.hash_reused is not verify_hashes for change in changes.changes)
        assert changes.summary.unchanged_files == 3
        assert _database_snapshot(catalog.database_path) == database_before
        assert [(p.files_observed, p.total_bytes_observed) for p in progress] == [(1, 0), (2, 4), (3, 8)]
        assert {p.current_relative_path.as_posix() for p in progress} == {"empty", "nested/copy", "original"}
        assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before
        for path in files:
            path.unlink()
        hashed.clear()
        progress.clear()
        removed = catalog.reconcile_directory(
            root, verify_hashes=verify_hashes, excluded_directories=(excluded,), progress_callback=progress.append
        )
        assert removed.summary.missing_files == 3
        assert hashed == []
        assert progress == []
        assert catalog.apply_refresh(removed).files_observed == 0
        assert (
            catalog.scan_directory(root, verify_hashes=verify_hashes, excluded_directories=(excluded,)).files_observed
            == 0
        )


def test_verification_detects_metadata_preserving_rewrite_and_preserves_history(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "entry").write_bytes(b"first")
    entry = root / "entry"
    entry.write_bytes(b"first")
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog:
        catalog.scan_directory(root)
        catalog.scan_directory(other)
        original_scan = catalog.current_scan(root)
        original_history = tuple(catalog.observation_history())
        other_scan = catalog.current_scan(other)
        original = catalog.files(root).first()
        assert original is not None
        entry.write_bytes(b"later")
        os.utime(entry, ns=(original.mtime_ns, original.mtime_ns))
        assert entry.stat().st_mtime_ns == original.mtime_ns
        cached = catalog.reconcile_directory(root)
        assert cached.changes[0].hash_reused
        assert cached.summary.unchanged_files == 1
        database_before = _database_snapshot(database)
        verified = catalog.reconcile_directory(root, verify_hashes=True)
        assert _database_snapshot(database) == database_before
        assert verified.summary.modified_files == 1
        assert not verified.changes[0].hash_reused
        assert catalog.current_scan(root) == original_scan
        assert tuple(catalog.observation_history()) == original_history
        catalog.scan_directory(root, verify_hashes=True)
        current = catalog.files(root).first()
        assert current is not None and current.content_id.digest == sha256(b"later").hexdigest()
        assert catalog.current_scan(other) == other_scan
        history = tuple(catalog.observation_history())
        assert all(observation in history for observation in original_history)
        before = (entry.read_bytes(), entry.stat().st_size, entry.stat().st_mtime_ns)
    with Catalog.open(database) as catalog:
        assert catalog.files(root).first() == current
        assert tuple(catalog.observation_history()) == history
    assert (entry.read_bytes(), entry.stat().st_size, entry.stat().st_mtime_ns) == before


@pytest.mark.parametrize("failure", ["read", "mutation", "callback", "interrupt"])
def test_verified_refresh_failure_preserves_snapshot_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry"
    entry.write_bytes(b"stable")
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog:
        catalog.scan_directory(root)
        current = catalog.files(root).fetch()
        history = tuple(catalog.observation_history())
        scans = tuple(catalog.scan_history())
        before = (entry.read_bytes(), entry.stat().st_mtime_ns)

        def fail_hash(path: Path) -> tuple[ContentId, int, int]:
            if failure == "mutation":
                raise FileChangedDuringHashingError("controlled mutation")
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise OSError("controlled read failure")

        def fail_callback(progress: ScanProgress) -> None:
            raise RuntimeError("controlled callback failure")

        if failure != "callback":
            monkeypatch.setattr("archiver.catalog.hash_file_stably", fail_hash)
        expected = KeyboardInterrupt if failure == "interrupt" else RefreshFailure
        with pytest.raises(expected):
            catalog.scan_directory(
                root, verify_hashes=True, progress_callback=fail_callback if failure == "callback" else None
            )
        assert catalog.files(root).fetch() == current
        assert tuple(catalog.observation_history()) == history
        assert tuple(catalog.scan_history()) == scans
        assert (entry.read_bytes(), entry.stat().st_mtime_ns) == before
    with Catalog.open(database) as catalog:
        assert catalog.files(root).fetch() == current
        assert tuple(catalog.scan_history()) == scans


@pytest.mark.parametrize("command", ["refresh", "scan"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_verifies_matching_metadata_and_preserves_preview(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, dry_run: bool
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry"
    entry.write_bytes(b"first")
    (root / "stable").write_bytes(b"stable")
    assert main(["catalog", "init", str(root)]) == 0
    assert main(["catalog", "refresh", str(root)]) == 0
    database = root / ".archiver" / "catalog.sqlite"
    with Catalog.open(database) as catalog:
        scans = tuple(catalog.scan_history())
        history = tuple(catalog.observation_history())
    database_before = _database_snapshot(database)
    metadata = entry.stat()
    entry.write_bytes(b"later")
    os.utime(entry, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    capsys.readouterr()
    args = ["catalog", command, str(root), "--verify-hashes", "--details", "all", "--no-progress"]
    if dry_run:
        args.append("--dry-run")
    assert main(args) == 0
    output = capsys.readouterr()
    assert "MODIFIED  entry" in output.out
    assert "UNCHANGED stable (hash rechecked)" in output.out
    assert "New files: 0" in output.out  # .archiver stays excluded
    assert output.err == ""
    with Catalog.open(database) as catalog:
        current = catalog.files(root).first()
        assert current is not None
        assert current.content_id.digest == sha256(b"first" if dry_run else b"later").hexdigest()
        if dry_run:
            assert _database_snapshot(database) == database_before
            assert tuple(catalog.scan_history()) == scans
            assert tuple(catalog.observation_history()) == history


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_metadata_only_change_and_rename_keep_content_identity(tmp_path: Path, verify_hashes: bool) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry"
    entry.write_bytes(b"stable")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        original = catalog.files(root).first()
        assert original is not None
        updated_mtime = original.mtime_ns + 1_000_000_000
        os.utime(entry, ns=(updated_mtime, updated_mtime))
        change_set = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        change = change_set.changes[0]
        assert change.kind == "unchanged" and not change.hash_reused
        assert change.current is not None
        assert change.current.content_id == original.content_id
        assert change.current.mtime_ns == entry.stat().st_mtime_ns
        catalog.apply_refresh(change_set)
        entry.rename(root / "renamed")
        renamed = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        assert [(c.relative_path.as_posix(), c.kind) for c in renamed.changes] == [
            ("entry", "missing"),
            ("renamed", "new"),
        ]
        assert renamed.changes[1].current is not None
        assert renamed.changes[1].current.content_id == original.content_id


@pytest.mark.parametrize("command", ["refresh", "scan"])
def test_cli_verification_help_and_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["catalog", command, "--help"])
    assert help_exit.value.code == 0
    assert "--verify-hashes" in capsys.readouterr().out
    root = tmp_path / "source"
    root.mkdir()
    (root / "entry").write_bytes(b"stable")
    assert main(["catalog", "init", str(root)]) == 0
    assert main(["catalog", command, str(root)]) == 0
    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        scan = catalog.current_scan(root)

    def fail_hash(path: Path) -> tuple[ContentId, int, int]:
        raise OSError("controlled read failure")

    monkeypatch.setattr("archiver.catalog.hash_file_stably", fail_hash)
    capsys.readouterr()
    assert main(["catalog", command, str(root), "--verify-hashes"]) == 1
    output = capsys.readouterr()
    assert "Refresh completed" not in output.out
    assert "reconciliation failed" in output.err
    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        assert catalog.current_scan(root) == scan
