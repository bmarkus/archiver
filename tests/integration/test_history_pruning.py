from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import closing
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from archiver import (
    Catalog,
    DuplicateGroupHandle,
    HistoryPruningError,
    PrunedScanError,
    ScanHistorySummary,
    TagProvenance,
)


def _dump(database: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.iterdump())


def _seed(database: Path, root: Path) -> None:
    """The plan's exact cutoff example; failed/running totals are partial."""
    root.mkdir(exist_ok=True)
    with Catalog.create(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("INSERT INTO locations(id, root_path) VALUES (1, ?)", (str(root.resolve()),))
        connection.execute("INSERT INTO content VALUES (1, 'sha256', ?, 4)", ("a" * 64,))
        statuses = ["failed", "completed", "failed", "completed", "running", "failed", "completed", "failed"]
        for scan_id, status in enumerate(statuses, 1):
            # Ties and reversed timestamps deliberately disagree with ID order.
            connection.execute(
                "INSERT INTO scan_runs VALUES (?, 1, ?, ?, ?)",
                (scan_id, 0 if scan_id % 2 else 8 - scan_id, None if status == "running" else 0, status),
            )
            if scan_id != 4:  # Empty successful snapshot must still be retained.
                connection.execute(
                    "INSERT INTO file_observations(scan_id, relative_path, content_id, size_bytes, mtime_ns) "
                    "VALUES (?, 'entry', 1, 4, 0)",
                    (scan_id,),
                )
        connection.execute("UPDATE locations SET current_scan_id = 7")
        connection.commit()


@pytest.mark.parametrize(
    ("keep_last", "include_failed", "removed"),
    [
        (1, False, {2, 4}),
        (1, True, {1, 2, 3, 4, 6}),
        (2, False, {2}),
        (2, True, {1, 2, 3}),
        (3, False, set()),
        (3, True, {1}),
        (5, True, {1}),
        (2**63 - 1, False, set()),
    ],
)
def test_retention_cutoff_counts_and_reopen(
    tmp_path: Path, keep_last: int, include_failed: bool, removed: set[int]
) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)
    before = _dump(database)
    with Catalog.open(database) as catalog:
        preview = catalog.prune_history(root, keep_last=keep_last, include_failed=include_failed)
        assert preview.dry_run
        assert preview.scans_to_remove == len(removed)
        assert preview.observations_to_remove == len(removed - {4})
        assert preview.successful_scans_to_remove == len(removed & {2, 4, 7})
        assert preview.failed_scans_to_remove == len(removed & {1, 3, 6, 8})
        assert preview.scans_to_remove == preview.successful_scans_to_remove + preview.failed_scans_to_remove
        assert preview.successful_scans_retained == min(keep_last, 3)
        assert preview.oldest_retained_scan_id == {1: 7, 2: 4}.get(keep_last, 2)
        assert not catalog._connection.in_transaction
        assert _dump(database) == before
        current = catalog.files(root).fetch()
        summaries = {item.scan.id: item for item in catalog.scan_summaries(root)}
        applied = catalog.prune_history(root, keep_last=keep_last, include_failed=include_failed, dry_run=False)
        assert applied == replace(preview, dry_run=False)
        assert catalog.files(root).fetch() == current
        assert {item.scan.id: item for item in catalog.scan_summaries(root)} == {
            scan_id: item for scan_id, item in summaries.items() if scan_id not in removed
        }
        assert not catalog._connection.in_transaction
        assert list(catalog._connection.execute("PRAGMA foreign_key_check")) == []
        assert catalog.prune_history(root, keep_last=keep_last, include_failed=include_failed).scans_to_remove == 0
    with Catalog.open(database) as catalog:
        assert {scan.id for scan in catalog.scan_history()} == set(range(1, 9)) - removed
        assert {obs.scan.id for obs in catalog.observation_history()} == set(range(1, 9)) - removed - {4}
        assert catalog.files(root).fetch() == current


def test_summary_aggregates_are_lazy_physical_totals_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)

    def no_scan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("history inspection must not scan source files")

    with Catalog.open(database) as catalog:
        statements: list[str] = []
        catalog._connection.set_trace_callback(statements.append)
        rows = cast(Generator[ScanHistorySummary, None, None], catalog.scan_summaries(root))
        assert statements == []
        before = _dump(database)
        with monkeypatch.context() as context:
            context.setattr("archiver.catalog.regular_files", no_scan)
            context.setattr("archiver.catalog.hash_file_stably", no_scan)
            with closing(rows):
                values = tuple(rows)
        assert [v.scan.id for v in values] == list(range(8, 0, -1))
        assert [(v.files_observed, v.total_bytes_observed) for v in values] == [(1, 4)] * 4 + [(0, 0)] + [(1, 4)] * 3
        assert [v.scan.id for v in values if v.is_current] == [7]
        assert values[0].scan.status == "failed" and values[3].scan.completed_at_ns is None
        assert sum("LEFT JOIN file_observations" in statement for statement in statements) == 1
        assert sum(statement.lstrip().upper().startswith("SELECT") for statement in statements) == 2  # root + aggregate
        assert _dump(database) == before

    physical_root, physical_database = tmp_path / "physical", tmp_path / "physical.sqlite"
    (physical_root / "nested").mkdir(parents=True)
    (physical_root / "copy").write_bytes(b"same")
    (physical_root / "nested/copy").write_bytes(b"same")
    (physical_root / "empty").touch()
    with Catalog.create(physical_database) as catalog:
        catalog.scan_directory(physical_root)
        item = next(catalog.scan_summaries(physical_root))
        assert (item.files_observed, item.total_bytes_observed) == (3, 8)


def test_pruning_preserves_content_tags_other_roots_and_refresh_baseline(tmp_path: Path) -> None:
    root, other, database = tmp_path / "source", tmp_path / "other", tmp_path / "catalog.sqlite"
    (root / "nested").mkdir(parents=True)
    other.mkdir()
    source = root / "nested/entry"
    source.write_bytes(b"historical only")
    (other / "copy").write_bytes(b"historical only")
    provenance = TagProvenance("user", "tests", "1")
    with Catalog.create(database) as catalog:
        catalog.scan_directory(root)
        historical = catalog.files(root).first()
        assert historical is not None
        catalog.add_content_tag(historical.content_id, "keep", provenance)
        catalog.add_content_tag(historical.content_id, "retract", provenance)
        catalog.retract_content_tag(historical.content_id, "retract", provenance)
        catalog.add_content_tag(historical.content_id, "system", TagProvenance("system", "tool", "2", "method"))
        source.write_bytes(b"current content")
        (root / "empty").touch()
        catalog.scan_directory(root)
        catalog.add_tag_for_path(root, PurePosixPath("nested/entry"), "current", provenance)
        # Keep the historical identity exclusively in the pruned scan.
        (other / "copy").write_bytes(b"other content")
        catalog.scan_directory(other)
        catalog.scan_directory(root)
        old_max_id = max(row.id for row in catalog.scan_history())
        preserved_tables = {
            table: tuple(catalog._connection.execute(f"SELECT * FROM {table} ORDER BY id"))
            for table in ("content", "tags", "content_tag_assertions", "locations")
        }
        other_rows = tuple(catalog.scan_summaries(other))
        current = catalog.files(root).fetch()
        current_summary = catalog.current_summary(root)
        duplicates = catalog.duplicates(root).summary()
        tags = catalog.tags_for_path(root, PurePosixPath("nested/entry"))
        observed_source = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
        changes = catalog.reconcile_directory(root)
        catalog.prune_history(root, keep_last=1, dry_run=False)
        for table, rows in preserved_tables.items():
            assert tuple(catalog._connection.execute(f"SELECT * FROM {table} ORDER BY id")) == rows
        assert catalog.files(root).fetch() == current
        assert catalog.current_summary(root) == current_summary
        assert catalog.duplicates(root).summary() == duplicates
        assert catalog.tags_for_path(root, PurePosixPath("nested/entry")) == tags
        assert tuple(catalog.scan_summaries(other)) == other_rows
        assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in observed_source} == observed_source
        # Pruning never changes the current baseline or silently enables auto-pruning.
        catalog.apply_refresh(changes)
        catalog.scan_directory(root)
        assert len(tuple(catalog.scan_summaries(root))) == 3
        assert max(row.id for row in catalog.scan_history()) > old_max_id
    with Catalog.open(database) as catalog:
        assert catalog.tags_for_content(historical.content_id)
        assert catalog.files(root).fetch()


@pytest.mark.parametrize("dry_run", [False, True])
def test_unknown_unscanned_empty_and_only_failed_roots(tmp_path: Path, dry_run: bool) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    root.mkdir()
    with Catalog.create(database) as catalog:
        before = _dump(database)
        assert tuple(catalog.scan_summaries(root)) == ()
        assert catalog.prune_history(root, dry_run=dry_run).scans_to_remove == 0
        assert _dump(database) == before
        catalog._connection.execute("INSERT INTO locations(id,root_path) VALUES(1,?)", (str(root.resolve()),))
        catalog._connection.commit()
        assert catalog.prune_history(root, include_failed=True, dry_run=dry_run).oldest_retained_scan_id is None
        catalog._connection.execute("INSERT INTO scan_runs VALUES(1,1,0,0,'failed')")
        catalog._connection.commit()
        summary = catalog.prune_history(root, include_failed=True, dry_run=dry_run)
        assert summary.oldest_retained_scan_id is None and summary.scans_to_remove == 0
        catalog.scan_directory(root)
        assert catalog.prune_history(root, include_failed=True).failed_scans_to_remove == 1
        catalog.scan_directory(root)
        assert catalog.prune_history(root, keep_last=1, dry_run=dry_run).observations_to_remove == 0


@pytest.mark.parametrize("bad", [True, 1.5, "3", None, 0, -1, 2**63])
def test_pruning_validates_before_reads(tmp_path: Path, bad: Any) -> None:
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        statements: list[str] = []
        catalog._connection.set_trace_callback(statements.append)
        with pytest.raises(TypeError if type(bad) is not int else ValueError):
            catalog.prune_history(tmp_path / "nonexistent", keep_last=bad)
        assert statements == []


@pytest.mark.parametrize("keyword", ["dry_run", "include_failed"])
def test_pruning_rejects_non_bool_options(tmp_path: Path, keyword: str) -> None:
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        with pytest.raises(TypeError):
            catalog.prune_history(tmp_path, **{keyword: 1})  # type: ignore[arg-type]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("cross_location", [False, True])
def test_referenced_old_scan_is_never_deleted(tmp_path: Path, dry_run: bool, cross_location: bool) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)
    with Catalog.open(database) as catalog:
        if cross_location:
            catalog._connection.execute("INSERT INTO locations(root_path,current_scan_id) VALUES('other',2)")
        else:
            catalog._connection.execute("UPDATE locations SET current_scan_id=2 WHERE id=1")
        catalog._connection.commit()
        before = _dump(database)
        with pytest.raises(HistoryPruningError, match="current scan"):
            catalog.prune_history(root, keep_last=1, dry_run=dry_run, include_failed=True)
        assert _dump(database) == before
        assert not catalog._connection.in_transaction


class _FaultConnection(sqlite3.Connection):
    fault: str | None = None
    child_deleted: bool = False

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if self.fault == "read" and "retained_count" in sql:
            raise sqlite3.OperationalError("injected reporting failure")
        if "DELETE FROM scan_runs" in sql and self.fault in ("parent", "interrupt"):
            assert self.child_deleted
            if self.fault == "interrupt":
                raise KeyboardInterrupt
            raise sqlite3.OperationalError("injected parent deletion failure")
        result = super().execute(sql, parameters)
        if "DELETE FROM file_observations" in sql:
            self.child_deleted = True
        return result

    def commit(self) -> None:
        if self.fault == "commit":
            raise sqlite3.OperationalError("injected commit failure")
        super().commit()


class _TrackedCursor(sqlite3.Cursor):
    closed: bool = False
    fail_next: bool = False

    def __next__(self) -> Any:
        if self.fail_next:
            raise sqlite3.OperationalError("injected summary iteration failure")
        return super().__next__()

    def close(self) -> None:
        self.closed = True
        super().close()


class _TrackedConnection(sqlite3.Connection):
    summary_cursor: _TrackedCursor | None = None

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if "LEFT JOIN file_observations" in sql:
            cursor = self.cursor(factory=_TrackedCursor)
            assert isinstance(cursor, _TrackedCursor)
            self.summary_cursor = cursor
            return cursor.execute(sql, parameters)
        return super().execute(sql, parameters)


@pytest.mark.parametrize("failure", [False, True])
def test_summary_cursor_closes_after_early_exit_or_iteration_error(tmp_path: Path, failure: bool) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)
    connection = sqlite3.connect(database, factory=_TrackedConnection)
    with Catalog(database, connection) as catalog:
        rows = cast(Generator[ScanHistorySummary, None, None], catalog.scan_summaries(root))
        assert next(rows).scan.id == 8
        cursor = connection.summary_cursor
        assert cursor is not None and not cursor.closed
        if failure:
            cursor.fail_next = True
            with pytest.raises(sqlite3.OperationalError):
                next(rows)
        else:
            rows.close()
        assert cursor.closed
        assert catalog.prune_history(root, keep_last=1).scans_to_remove == 2


def test_failure_cutoff_is_recomputed_after_new_success_and_ids_are_not_reused(tmp_path: Path) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)
    with Catalog.open(database) as catalog:
        old_report = catalog.prune_history(root, keep_last=2, include_failed=True)
        assert old_report.oldest_retained_scan_id == 4
        assert old_report.failed_scans_to_remove == 2
        catalog.scan_directory(root)
        new_report = catalog.prune_history(root, keep_last=2, include_failed=True)
        assert new_report.oldest_retained_scan_id == 7
        assert new_report.failed_scans_to_remove == 3
        catalog.prune_history(root, keep_last=2, include_failed=True, dry_run=False)
        catalog.scan_directory(root)
        assert max(row.id for row in catalog.scan_history()) == 10


def test_newer_failed_maximum_id_is_preserved(tmp_path: Path) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)
    with Catalog.open(database) as catalog:
        catalog.prune_history(root, keep_last=1, include_failed=True, dry_run=False)
        assert max(row.id for row in catalog.scan_history()) == 8
        catalog.scan_directory(root)
        assert max(row.id for row in catalog.scan_history()) == 9


@pytest.mark.parametrize("fault", ["read", "parent", "interrupt", "commit"])
def test_pruning_failure_rolls_back_and_connection_remains_usable(tmp_path: Path, fault: str) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)
    before = _dump(database)
    connection = sqlite3.connect(database, factory=_FaultConnection)
    with Catalog(database, connection) as catalog:
        connection.fault = fault
        with pytest.raises(KeyboardInterrupt if fault == "interrupt" else HistoryPruningError):
            catalog.prune_history(root, keep_last=1, dry_run=fault == "read", include_failed=True)
        assert _dump(database) == before
        assert not connection.in_transaction
        connection.fault = None
        assert catalog.prune_history(root, keep_last=1, dry_run=False).scans_to_remove == 2
    with Catalog.open(database) as catalog:
        assert len(tuple(catalog.scan_history())) == 6


@pytest.mark.parametrize("dry_run", [False, True])
def test_caller_transaction_is_not_touched(tmp_path: Path, dry_run: bool) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)
    before = _dump(database)
    with Catalog.open(database) as catalog:
        catalog._connection.execute("UPDATE scan_runs SET started_at_ns=123 WHERE id=1")
        with pytest.raises(HistoryPruningError, match="existing transaction"):
            catalog.prune_history(root, dry_run=dry_run)
        assert catalog._connection.in_transaction
        assert catalog._connection.execute("SELECT started_at_ns FROM scan_runs WHERE id=1").fetchone()[0] == 123
        assert _dump(database) == before
        catalog._connection.rollback()


def test_writer_contention_does_not_change_state(tmp_path: Path) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    _seed(database, root)
    before = _dump(database)
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with Catalog(database, sqlite3.connect(database, timeout=0)) as catalog:
            with pytest.raises(HistoryPruningError) as failure:
                catalog.prune_history(root, keep_last=1, dry_run=False)
            assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
            assert not catalog._connection.in_transaction
            assert (
                catalog.prune_history(root, keep_last=1).scans_to_remove == 2
            )  # read-only snapshot needs no writer lock
            writer.rollback()
            assert catalog.prune_history(root, keep_last=1, dry_run=False).scans_to_remove == 2
    assert _dump(database) != before


def test_duplicate_handles_survive_retention_but_fail_after_pruning(tmp_path: Path) -> None:
    root, database = tmp_path / "source", tmp_path / "catalog.sqlite"
    root.mkdir()
    (root / "one").write_bytes(b"duplicate")
    (root / "two").write_bytes(b"duplicate")
    with Catalog.create(database) as catalog:
        catalog.scan_directory(root)
        with closing(cast(Generator[DuplicateGroupHandle, None, None], catalog.duplicates(root).stream())) as groups:
            old_handle = next(groups)
        deferred_members = old_handle.stream_all_members()
        catalog.scan_directory(root)
        assert len(tuple(old_handle.stream_all_members())) == 2
        catalog.prune_history(root, keep_last=2, dry_run=False)
        assert len(tuple(old_handle.stream_all_members())) == 2
        with closing(cast(Generator[DuplicateGroupHandle, None, None], catalog.duplicates(root).stream())) as groups:
            current_handle = next(groups)
        catalog.prune_history(root, keep_last=1, dry_run=False)
        with pytest.raises(PrunedScanError):
            next(deferred_members)
        with pytest.raises(PrunedScanError):
            tuple(old_handle.stream_members())
        assert len(tuple(current_handle.stream_all_members())) == 2
        closed_members = current_handle.stream_members()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        next(closed_members)
