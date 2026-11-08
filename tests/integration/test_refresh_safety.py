from __future__ import annotations

import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from archiver import Catalog, RefreshFailure, StaleRefreshError

_TABLES = ("locations", "content", "scan_runs", "file_observations")


def _table_counts(database_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(database_path)
    try:
        counts: dict[str, int] = {}
        for table in _TABLES:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert row is not None
            counts[table] = int(row[0])
        return counts
    finally:
        connection.close()


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_apply_refresh_rolls_back_after_late_duplicate_path_failure(tmp_path: Path, verify_hashes: bool) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "seed.txt").write_bytes(b"seed")
    database_path = tmp_path / "catalog.sqlite"

    with Catalog.create(database_path) as catalog:
        catalog.scan_directory(root, verify_hashes=verify_hashes)
        expected_current = list(catalog.files(root).stream_all())
        expected_scans = list(catalog.scan_history())
        expected_history = list(catalog.observation_history())
        expected_counts = _table_counts(database_path)
        (root / "a-first.txt").write_bytes(b"first")
        (root / "b-second.txt").write_bytes(b"second")
        change_set = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        first = next(change for change in change_set.changes if change.relative_path.as_posix() == "a-first.txt")
        assert first.current is not None

        forged_changes = []
        for change in change_set.changes:
            if change.relative_path.as_posix() == "b-second.txt":
                assert change.current is not None
                forged_changes.append(
                    replace(change, current=replace(change.current, relative_path=first.current.relative_path))
                )
            else:
                forged_changes.append(change)
        forged_change_set = replace(change_set, changes=tuple(forged_changes))

        with pytest.raises(RefreshFailure, match="could not apply refresh"):
            catalog.apply_refresh(forged_change_set)

        assert list(catalog.files(root).stream_all()) == expected_current
        assert list(catalog.scan_history()) == expected_scans
        assert list(catalog.observation_history()) == expected_history
        assert _table_counts(database_path) == expected_counts

    with Catalog.open(database_path) as reopened:
        assert list(reopened.files(root).stream_all()) == expected_current
        assert list(reopened.scan_history()) == expected_scans
        assert list(reopened.observation_history()) == expected_history
        assert _table_counts(database_path) == expected_counts


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_apply_refresh_rolls_back_contradictory_content_size(tmp_path: Path, verify_hashes: bool) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "seed.txt").write_bytes(b"seed")
    database_path = tmp_path / "catalog.sqlite"

    with Catalog.create(database_path) as catalog:
        catalog.scan_directory(root, verify_hashes=verify_hashes)
        seed = catalog.files(root).first()
        assert seed is not None
        expected_current = list(catalog.files(root).stream_all())
        expected_counts = _table_counts(database_path)
        (root / "a-first.txt").write_bytes(b"first")
        (root / "z-conflict.txt").write_bytes(b"conflict")
        change_set = catalog.reconcile_directory(root, verify_hashes=verify_hashes)

        forged_changes = []
        for change in change_set.changes:
            if change.relative_path.as_posix() == "z-conflict.txt":
                assert change.current is not None
                forged_changes.append(
                    replace(
                        change,
                        current=replace(
                            change.current,
                            content_id=seed.content_id,
                            size_bytes=seed.size_bytes + 1,
                        ),
                    )
                )
            else:
                forged_changes.append(change)
        forged_change_set = replace(change_set, changes=tuple(forged_changes))

        with pytest.raises(RefreshFailure, match="contradictory sizes"):
            catalog.apply_refresh(forged_change_set)

        assert list(catalog.files(root).stream_all()) == expected_current
        assert _table_counts(database_path) == expected_counts

    with Catalog.open(database_path) as reopened:
        assert list(reopened.files(root).stream_all()) == expected_current
        assert _table_counts(database_path) == expected_counts


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_stale_refresh_application_creates_no_rows(tmp_path: Path, verify_hashes: bool) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "entry.txt").write_bytes(b"contents")
    database_path = tmp_path / "catalog.sqlite"

    with Catalog.create(database_path) as catalog:
        first_change_set = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        catalog.apply_refresh(first_change_set)
        stale_change_set = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        catalog.scan_directory(root, verify_hashes=verify_hashes)
        expected_current = list(catalog.files(root).stream_all())
        expected_scans = list(catalog.scan_history())
        expected_history = list(catalog.observation_history())
        expected_counts = _table_counts(database_path)

        with pytest.raises(StaleRefreshError, match="no longer matches"):
            catalog.apply_refresh(stale_change_set)

        assert list(catalog.files(root).stream_all()) == expected_current
        assert list(catalog.scan_history()) == expected_scans
        assert list(catalog.observation_history()) == expected_history
        assert _table_counts(database_path) == expected_counts


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_apply_refresh_persists_captured_snapshot_then_detects_later_source_change(
    tmp_path: Path, verify_hashes: bool
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry.txt"
    entry.write_bytes(b"before")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        change_set = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        entry.write_bytes(b"after")
        catalog.apply_refresh(change_set)

        current = list(catalog.files(root).stream_all())
        assert len(current) == 1
        assert current[0].content_id.digest == sha256(b"before").hexdigest()
        assert current[0].size_bytes == len(b"before")

        later_change_set = catalog.reconcile_directory(root, verify_hashes=verify_hashes)
        assert [(change.relative_path.as_posix(), change.kind) for change in later_change_set.changes] == [
            ("entry.txt", "modified")
        ]
        assert later_change_set.changes[0].current is not None
        assert later_change_set.changes[0].current.content_id.digest == sha256(b"after").hexdigest()
