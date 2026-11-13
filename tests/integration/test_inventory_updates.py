from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from contextlib import closing
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

import archiver.catalog as catalog_module
from archiver import (
    Catalog,
    DuplicateGroupHandle,
    InvalidCatalogError,
    InventoryRevision,
    PrunedScanError,
    RefreshFailure,
    StaleRefreshError,
    TagProvenance,
)
from archiver.cli import main
from archiver.hashing import hash_file_stably
from archiver.models import ContentId


def _paths(catalog: Catalog, root: Path) -> set[str]:
    return {item.relative_path.as_posix() for item in catalog.files(root).stream_all()}


def _dump(catalog: Catalog) -> tuple[str, ...]:
    return tuple(catalog._connection.iterdump())


def test_mixed_updates_history_queries_refresh_and_reopen(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "keep").write_bytes(b"same")
    (root / "remove").write_bytes(b"same")
    (root / "modify").write_bytes(b"old")
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog:
        catalog.scan_directory(root)
        history = tuple(catalog.observation_history())
        identity = catalog.content_for_path(root, PurePosixPath("remove"))
        catalog.add_content_tag(identity, "remember", TagProvenance("user", "test", "1"))
        (root / "nested").mkdir()
        (root / "nested/empty").write_bytes(b"")
        (root / "modify").write_bytes(b"changed bytes")
        (root / "duplicate").write_bytes(b"same")
        files = [root / "modify", root / "nested/empty", root / "duplicate"]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in root.rglob("*") if p.is_file()}
        baseline = _dump(catalog)
        changes = catalog.prepare_file_update(root, paths=files, remove_paths=[PurePosixPath("remove")])
        assert _dump(catalog) == baseline
        result = catalog.apply_inventory_changes(changes)
        assert (result.inserted_files, result.replaced_files, result.removed_files) == (2, 1, 1)
        assert result.inventory == catalog.current_summary(root)
        assert result.retention is not None and result.retention.successful_scans_retained == 2
        assert _paths(catalog, root) == {"keep", "modify", "nested/empty", "duplicate"}
        assert catalog.duplicates(root).summary().duplicate_content_group_count == 1
        assert catalog.tags_for_path(root, PurePosixPath("duplicate"))[0].tag == "remember"
        assert catalog.contents().count() == 4  # old content remains known
        assert tuple(catalog.observation_history())[: len(history)] == history
        assert [row.origin for row in catalog.scan_history()] == ["full_scan", "targeted_update"]
        assert {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in before} == before
    with Catalog.open(database) as catalog:
        assert "remove" not in _paths(catalog, root)
        assert catalog.current_scan(root) is not None
        catalog.scan_directory(root, keep_last=2)
        assert "remove" in _paths(catalog, root)  # retirement never deleted it
        assert len(tuple(catalog.scan_history())) == 2


def test_initial_registration_no_scan_empty_and_unchanged_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "known").write_bytes(b"same")
    (root / "unknown").write_bytes(b"not registered")
    calls: list[Path] = []
    original = hash_file_stably

    def hashed(path: Path) -> tuple[ContentId, int, int]:
        calls.append(path)
        return original(path)

    def no_walk(*args: Any) -> None:
        raise AssertionError("targeted work must not traverse")

    monkeypatch.setattr(catalog_module, "regular_files", no_walk)
    monkeypatch.setattr(catalog_module, "hash_file_stably", hashed)
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        before = _dump(catalog)
        result = catalog.apply_inventory_changes(catalog.prepare_file_update(root))
        assert result.revision == InventoryRevision(catalog.catalog_uuid, root.resolve(), None)
        assert result.inventory is None
        assert result.retention is None
        assert result.origin is None
        assert _dump(catalog) == before
        first = catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=Path("known")))
        assert _paths(catalog, root) == {"known"}
        second = catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=Path("known")))
        assert calls == [root / "known"]
        assert second.replaced_files == 1 and first.revision != second.revision
        catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=Path("known"), verify_hashes=True))
        assert len(calls) == 2 and len(tuple(catalog.scan_history())) == 2


@pytest.mark.parametrize("keep_last", [1, 2, 4, None])
def test_retention_preserves_other_roots_tags_and_incomplete_runs(tmp_path: Path, keep_last: int | None) -> None:
    root, other = tmp_path / "root", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (root / "one").write_bytes(b"same")
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog:
        catalog.scan_directory(other)
        for _ in range(3):
            catalog.scan_directory(root)
        identity = catalog.content_for_path(root, PurePosixPath("one"))
        catalog.add_content_tag(identity, "keep", TagProvenance("user", "test", "1"))
        location = catalog.current_scan(root)
        assert location is not None
        with catalog._connection:
            for status in ("failed", "running"):
                catalog._connection.execute(
                    "INSERT INTO scan_runs(location_id, started_at_ns, status) VALUES (?, 0, ?)",
                    (location.location.id, status),
                )
        catalog.apply_inventory_changes(
            catalog.prepare_file_update(root, remove_paths=[PurePosixPath("one")]),
            keep_last=keep_last,
        )
        rows = tuple(catalog.scan_summaries(root))
        assert sum(row.scan.status == "completed" for row in rows) == (4 if keep_last is None else keep_last)
        assert {row.scan.status for row in rows} == {"completed", "failed", "running"}
        assert len(tuple(catalog.scan_summaries(other))) == 1
        assert catalog.tags_for_content(identity)[0].tag == "keep"
        assert catalog.contents().count() == 1
        assert not _paths(catalog, root)


@pytest.mark.parametrize("full", [False, True])
def test_stale_baselines_and_foreign_scope(tmp_path: Path, full: bool) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"one")
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog, Catalog.create(tmp_path / "foreign.sqlite") as foreign:
        catalog.scan_directory(root)
        empty = catalog.prepare_file_update(root)
        changes = catalog.prepare_file_update(root, paths=Path("one"))
        refresh = catalog.reconcile_directory(root)
        with pytest.raises(StaleRefreshError):
            foreign.apply_inventory_changes(empty)
        different = replace(changes, baseline=replace(changes.baseline, _root=tmp_path))
        with pytest.raises(StaleRefreshError):
            catalog.apply_inventory_changes(different)
        with Catalog.open(database) as writer:
            if full:
                writer.scan_directory(root)
            else:
                writer.apply_inventory_changes(writer.prepare_file_update(root, paths=Path("one")))
        before = _dump(catalog)
        for candidate in (empty, changes):
            with pytest.raises(StaleRefreshError):
                catalog.apply_inventory_changes(candidate)
        with pytest.raises(StaleRefreshError):
            catalog.apply_refresh(refresh)
        assert _dump(catalog) == before


@pytest.mark.parametrize("full", [False, True])
def test_existing_transaction_untouched_and_lock_before_baseline(tmp_path: Path, full: bool) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"one")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        update = catalog.prepare_file_update(root, paths=Path("one"))
        refresh = catalog.reconcile_directory(root)
        catalog._connection.execute("INSERT INTO tags(name) VALUES ('pending')")
        before = _dump(catalog)
        with pytest.raises(RefreshFailure):
            if full:
                catalog.apply_refresh(refresh)
            else:
                catalog.apply_inventory_changes(update)
        assert catalog._connection.in_transaction and _dump(catalog) == before
        catalog._connection.rollback()
        statements: list[str] = []
        catalog._connection.set_trace_callback(statements.append)
        if full:
            catalog.apply_refresh(refresh)
        else:
            catalog.apply_inventory_changes(update)
        lock = next(i for i, sql in enumerate(statements) if sql == "BEGIN IMMEDIATE")
        baseline_read = next(i for i, sql in enumerate(statements) if "SELECT current_scan_id" in sql)
        assert lock < baseline_read


@pytest.mark.parametrize("fault", ["copy", "insert", "pointer", "retention", "interrupt"])
def test_publication_failure_atomicity_and_reusable_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"one")
    (root / "two").write_bytes(b"two")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        for _ in range(3):
            catalog.scan_directory(root)
        update = catalog.prepare_file_update(root, paths=Path("two"))
        before = _dump(catalog)
        if fault in {"copy", "insert", "pointer"}:
            event, table = ("UPDATE", "locations") if fault == "pointer" else ("INSERT", "file_observations")
            condition = "NEW.relative_path='two'" if fault == "insert" else "1"
            catalog._connection.execute(
                f"CREATE TEMP TRIGGER fail_publication BEFORE {event} ON {table} WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        else:
            original = catalog._delete_history

            def fail(*args: Any, **kwargs: Any) -> None:
                original(*args, **kwargs)
                if fault == "interrupt":
                    raise KeyboardInterrupt
                raise sqlite3.OperationalError("injected failure after pruning")

            monkeypatch.setattr(catalog, "_delete_history", fail)
        with pytest.raises(KeyboardInterrupt if fault == "interrupt" else RefreshFailure):
            catalog.apply_inventory_changes(update)
        assert _dump(catalog) == before
        assert not catalog._connection.in_transaction
        assert catalog._connection.execute("SELECT COUNT(*) FROM inventory_affected").fetchone()[0] == 0
        if fault in {"copy", "insert", "pointer"}:
            catalog._connection.execute("DROP TRIGGER fail_publication")
        else:
            monkeypatch.setattr(catalog, "_delete_history", original)
        catalog.apply_inventory_changes(update)
        assert len(tuple(catalog.scan_history())) == 2


def test_preparation_does_not_mix_baselines_during_hashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"one")
    (root / "two").write_bytes(b"two")
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog, Catalog.open(database) as writer:
        catalog.scan_directory(root)
        baseline = catalog.current_scan(root)
        original = hash_file_stably
        calls = 0

        def hashed(path: Path) -> tuple[ContentId, int, int]:
            nonlocal calls
            assert not catalog._connection.in_transaction
            if calls == 0:
                writer.scan_directory(root, keep_last=1)  # prune captured baseline during preparation
            calls += 1
            return original(path)

        monkeypatch.setattr(catalog_module, "hash_file_stably", hashed)
        update = catalog.prepare_file_update(root, paths=[Path("one"), Path("two")], verify_hashes=True)
        assert baseline is not None and update.baseline._snapshot_id == baseline.id
        with pytest.raises(StaleRefreshError):
            catalog.apply_inventory_changes(update)
        assert calls == 2 and len(tuple(catalog.scan_history())) == 1


def test_preparation_reads_one_snapshot_during_concurrent_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"one")
    (root / "two").write_bytes(b"two")
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog, Catalog.open(database) as writer:
        catalog._connection.execute("PRAGMA journal_mode=WAL").close()
        catalog.scan_directory(root)
        baseline = catalog.current_scan(root)
        assert baseline is not None
        original = catalog._baseline_file
        reads: list[int | None] = []

        def baseline_file(snapshot: int | None, path: PurePosixPath) -> Any:
            reads.append(snapshot)
            if len(reads) == 1:
                writer.scan_directory(root, keep_last=1)
            return original(snapshot, path)

        monkeypatch.setattr(catalog, "_baseline_file", baseline_file)
        changes = catalog.prepare_file_update(root, paths=[Path("one"), Path("two")])
        assert reads == [baseline.id, baseline.id]
        assert changes.baseline._snapshot_id == baseline.id
        assert len(changes._upserts) == 2  # the read snapshot still contained both cached records
        with pytest.raises(StaleRefreshError):
            catalog.apply_inventory_changes(changes)


def test_observation_failure_no_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"one")
    (root / "two").write_bytes(b"two")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        before = _dump(catalog)
        original = hash_file_stably

        def hashed(path: Path) -> tuple[ContentId, int, int]:
            if path.name == "two":
                raise OSError("unreadable")
            return original(path)

        monkeypatch.setattr(catalog_module, "hash_file_stably", hashed)
        with pytest.raises(OSError):
            catalog.prepare_file_update(root, paths=[Path("one"), Path("two")])
        assert _dump(catalog) == before


def test_large_batch_ignores_sql_placeholder_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    files = [root / str(i) for i in range(80)]
    for path in files:
        path.write_bytes(b"same")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog._connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 20)
        catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=files))
        result = catalog.apply_inventory_changes(
            catalog.prepare_file_update(root, remove_paths=[PurePosixPath(path.name) for path in files[:60]])
        )
        assert result.removed_files == 60 and len(_paths(catalog, root)) == 20


@pytest.mark.parametrize("bad", [True, 0, -1, 2**63, "2", 2.5])
def test_invalid_retention_before_writes(tmp_path: Path, bad: Any) -> None:
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        empty = catalog.prepare_file_update(tmp_path)
        before = _dump(catalog)
        with pytest.raises((ValueError, TypeError)):
            catalog.apply_inventory_changes(empty, keep_last=bad)
        with pytest.raises((ValueError, TypeError)):
            catalog.scan_directory(tmp_path, keep_last=bad)
        assert _dump(catalog) == before


def test_invalid_paths_and_forged_changes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"one")
    (tmp_path / "outside").write_bytes(b"outside")
    with Catalog.create(root / "catalog.sqlite") as catalog:
        catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=Path("one")))
        before = _dump(catalog)
        for paths in (
            [Path("one"), root / "one"],
            [tmp_path / "outside"],
            [Path(".")],
            [Path("../outside")],
            [Path("catalog.sqlite")],
            [Path("catalog.sqlite-wal")],
            [Path("missing")],
        ):
            with pytest.raises(ValueError):
                catalog.prepare_file_update(root, paths=paths)
        with pytest.raises(ValueError):
            catalog.prepare_file_update(root, paths=Path("one"), remove_paths=[PurePosixPath("one")])
        with pytest.raises(ValueError):
            catalog.prepare_file_update(root, remove_paths=[PurePosixPath("unknown")])
        update = catalog.prepare_file_update(root, paths=Path("one"))
        bad = replace(update, _upserts=(replace(update._upserts[0], size_bytes=-1),))
        with pytest.raises(ValueError):
            catalog.apply_inventory_changes(bad)
        assert _dump(catalog) == before


def test_case_aliases_and_hard_links(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "One").write_bytes(b"same")
    if not (root / "ONE").exists():
        pytest.skip("case-insensitive filesystem required")
    try:
        os.link(root / "One", root / "hardlink")
    except OSError:
        pytest.skip("hard links unavailable")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=Path("One")))
        catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=Path("ONE")))
        assert _paths(catalog, root) == {"One"}
        with pytest.raises(ValueError):
            catalog.prepare_file_update(root, paths=[Path("One"), Path("ONE")])
        with pytest.raises(ValueError):
            catalog.prepare_file_update(root, paths=Path("ONE"), remove_paths=[PurePosixPath("One")])
        catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=Path("hardlink")))
        assert _paths(catalog, root) == {"One", "hardlink"}


def test_missing_physical_removal_and_distinct_hardlinked_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"same")
    try:
        os.link(root / "one", root / "two")
    except OSError:
        pytest.skip("hard links unavailable")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=[Path("one"), Path("two")]))
        assert _paths(catalog, root) == {"one", "two"}
        (root / "two").unlink()  # simulate a successful physical operation by the future Archiver
        result = catalog.apply_inventory_changes(catalog.prepare_file_update(root, remove_paths=[PurePosixPath("two")]))
        assert result.removed_files == 1 and _paths(catalog, root) == {"one"}
        assert (root / "one").read_bytes() == b"same"


def test_symlink_ancestors_rejected_before_resolution(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    actual = root / "actual"
    actual.mkdir()
    (actual / "one").write_bytes(b"same")
    try:
        (root / "link").symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        with pytest.raises(ValueError, match="symbolic"):
            catalog.prepare_file_update(root, paths=Path("link/one"))
        with pytest.raises(ValueError, match="symbolic"):
            catalog.prepare_file_update(root / "link", paths=Path("one"))


def test_pinned_handles_and_origin_in_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    (root / ".archiver").mkdir(parents=True)
    (root / "one").write_bytes(b"same")
    (root / "two").write_bytes(b"same")
    with Catalog.create(root / ".archiver/catalog.sqlite") as catalog:
        catalog.scan_directory(root, excluded_directories=(root / ".archiver",))
        with closing(cast(Generator[DuplicateGroupHandle, None, None], catalog.duplicates(root).stream())) as groups:
            handle = next(groups)
        for i in range(2):
            catalog.apply_inventory_changes(catalog.prepare_file_update(root, paths=Path("one")))
            if i == 0:
                assert len(tuple(handle.stream_all_members())) == 2
        with pytest.raises(PrunedScanError):
            tuple(handle.stream_all_members())
    assert main(["catalog", "info", str(root)]) == 0
    assert "targeted_update" in capsys.readouterr().out
    assert main(["catalog", "history", str(root)]) == 0
    assert "targeted_update" in capsys.readouterr().out


def test_origin_constraint_and_schema_validation(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite"
    with Catalog.create(database) as catalog:
        assert catalog.schema_version == 4
        assert catalog._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        catalog.scan_directory(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            with catalog._connection:
                catalog._connection.execute("UPDATE scan_runs SET origin='invalid'")
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE scan_runs DROP COLUMN origin")
    before = database.read_bytes()
    with pytest.raises(InvalidCatalogError, match="origin"):
        Catalog.open(database)
    assert database.read_bytes() == before


@pytest.mark.parametrize("full", [False, True])
def test_atomic_retention_failure_for_both_origins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full: bool,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").write_bytes(b"one")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        for _ in range(3):
            catalog.scan_directory(root)
        refresh = catalog.reconcile_directory(root)
        targeted = catalog.prepare_file_update(root, paths=Path("one"))
        before = _dump(catalog)
        original = catalog._delete_history

        def fail(*args: Any, **kwargs: Any) -> None:
            original(*args, **kwargs)
            raise sqlite3.OperationalError("injected retention failure")

        monkeypatch.setattr(catalog, "_delete_history", fail)
        with pytest.raises(RefreshFailure):
            if full:
                catalog.apply_refresh(refresh, keep_last=2)
            else:
                catalog.apply_inventory_changes(targeted)
        assert _dump(catalog) == before
