from __future__ import annotations

import os
from pathlib import Path

import pytest

from archiver import Catalog, RefreshFailure, StaleRefreshError


def test_reconciliation_is_write_free_reuses_metadata_and_persists_metadata_only_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry.txt"
    entry.write_bytes(b"stable")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        initial = catalog.reconcile_directory(root)
        assert [change.kind for change in initial.changes] == ["new"]
        assert catalog.current_files(root) == []
        catalog.apply_refresh(initial)
        original = catalog.current_files(root)[0]

        def should_not_hash(_: Path) -> tuple[object, int, int]:
            raise AssertionError("metadata-matched files must reuse the known content identity")

        monkeypatch.setattr("archiver.catalog.hash_file_stably", should_not_hash)
        unchanged = catalog.reconcile_directory(root)
        assert unchanged.summary.unchanged_files == 1
        assert unchanged.changes[0].hash_reused is True
        monkeypatch.undo()

        os.utime(entry, ns=(original.mtime_ns + 1_000_000_000, original.mtime_ns + 1_000_000_000))
        metadata_only = catalog.reconcile_directory(root)
        change = metadata_only.changes[0]
        assert change.kind == "unchanged"
        assert change.hash_reused is False
        assert change.previous is not None and change.current is not None
        assert change.current.content_id == change.previous.content_id
        catalog.apply_refresh(metadata_only)
        assert catalog.current_files(root)[0].mtime_ns == original.mtime_ns + 1_000_000_000


def test_reconciliation_reports_new_modified_missing_and_rejects_stale_apply(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    retained = root / "retained.txt"
    changed = root / "changed.txt"
    removed = root / "removed.txt"
    retained.write_bytes(b"same")
    changed.write_bytes(b"first")
    removed.write_bytes(b"gone")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        removed.unlink()
        changed.write_bytes(b"second")
        (root / "new.txt").write_bytes(b"new")
        changes = catalog.reconcile_directory(root)
        assert [(change.relative_path.as_posix(), change.kind) for change in changes.changes] == [
            ("changed.txt", "modified"),
            ("new.txt", "new"),
            ("removed.txt", "missing"),
            ("retained.txt", "unchanged"),
        ]

        catalog.scan_directory(root)
        with pytest.raises(StaleRefreshError, match="no longer matches"):
            catalog.apply_refresh(changes)


def test_detectable_hash_failure_returns_no_change_set_and_preserves_current_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry.txt"
    entry.write_bytes(b"first")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        before = catalog.current_files(root)
        entry.write_bytes(b"second")

        def changed_during_hash(_: Path) -> tuple[object, int, int]:
            raise OSError("file changed during hashing")

        monkeypatch.setattr("archiver.catalog.hash_file_stably", changed_during_hash)
        with pytest.raises(RefreshFailure, match="reconciliation failed"):
            catalog.reconcile_directory(root)
        assert catalog.current_files(root) == before
