from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import archiver.catalog as catalog_module
import archiver.comparison as comparison_module
from archiver import (
    Catalog,
    ComparedContent,
    ContentComparisonError,
    ContentComparisonSummary,
    ContentId,
    InventoryRevision,
)


@pytest.fixture
def catalogs(tmp_path: Path) -> Generator[tuple[Catalog, Catalog, Path, Path], None, None]:
    source_root, target_root = tmp_path / "source", tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    with Catalog.create(tmp_path / "source.sqlite") as source, Catalog.create(tmp_path / "target.sqlite") as target:
        yield source, target, source_root, target_root


def _write(root: Path, name: str, data: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))


def _identity(data: bytes) -> ContentId:
    return ContentId("sha256", hashlib.sha256(data).hexdigest())


def _dump(catalog: Catalog) -> tuple[str, ...]:
    return tuple(catalog._connection.iterdump())


def test_overlap_representatives_reverse_and_reopen(catalogs: tuple[Catalog, Catalog, Path, Path]) -> None:
    source, target, sr, tr = catalogs
    for name, data in (("a/nested", b"duplicate"), ("a.zip", b"duplicate"), ("empty", b""), ("shared", b"shared")):
        _write(sr, name, data)
    for name, data in (("unrelated", b"target only"), ("z", b"shared"), ("nested/copy", b"shared")):
        _write(tr, name, data)
    # Different scan traversal orders give shared content different local IDs.
    source.scan_directory(sr)
    target.scan_directory(tr)
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        assert comparison.summary() == ContentComparisonSummary(3, 2, 1, 2, 1, 9)
        assert comparison.summary().bytes_to_copy == 9
        assert comparison.source_scope.has_inventory
        assert comparison.source_scope.location.root == sr.resolve()
        assert comparison.source_scope.revision == InventoryRevision(source.catalog_uuid, sr.resolve(), 1)
        missing = tuple(row for batch in comparison.batches(batch_size=1) for row in batch)
        assert {row.content_id for row in missing} == {_identity(b"duplicate"), _identity(b"")}
        duplicate = next(row for row in missing if row.content_id == _identity(b"duplicate"))
        assert duplicate.source_relative_path == PurePosixPath("a.zip")  # full BINARY, not component order
        assert duplicate.source_instance_count == 2
        assert duplicate.source_mtime_ns == (sr / "a.zip").stat().st_mtime_ns
        rows = tuple(row for batch in comparison.batches(include_present=True) for row in batch)
        assert [row.content_id.digest for row in rows] == sorted(row.content_id.digest for row in rows)
        assert {row.content_id for row in rows if row.in_target} == {_identity(b"shared")}
        saved_summary, saved_scope = comparison.summary(), comparison.source_scope
    assert saved_summary.source_content_count == 3 and comparison.source_scope == saved_scope
    with target.compare_content(source, source_root=tr, target_root=sr) as reverse:
        assert reverse.summary() == ContentComparisonSummary(2, 3, 1, 1, 2, 11)
    with Catalog.open(source.database_path) as reopened:
        with reopened.compare_content(target, source_root=sr, target_root=tr) as comparison:
            assert comparison.summary() == saved_summary


def test_scope_history_targeted_and_empty(catalogs: tuple[Catalog, Catalog, Path, Path], tmp_path: Path) -> None:
    source, target, sr, tr = catalogs
    _write(sr, "retire", b"old")
    source.scan_directory(sr)
    source.apply_inventory_changes(source.prepare_file_update(sr, remove_paths=[PurePosixPath("retire")]))
    _write(sr, "new", b"new")
    source.apply_inventory_changes(source.prepare_file_update(sr, paths=Path("new")))
    other = tmp_path / "other"
    other.mkdir()
    _write(other, "new", b"new")
    target.scan_directory(other)
    before = _dump(target)
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        assert comparison.summary() == ContentComparisonSummary(1, 0, 0, 1, 0, 3)
        assert not comparison.target_scope.has_inventory
        assert comparison.target_scope.location.id is None
        assert comparison.target_scope.revision == InventoryRevision(target.catalog_uuid, tr.resolve(), None)
    assert _dump(target) == before
    target.scan_directory(tr)
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        assert comparison.target_scope.has_inventory
        assert comparison.summary().target_content_count == 0
    # A registered location with no successful publication is also an empty known set.
    target._connection.execute("UPDATE locations SET current_scan_id = NULL WHERE root_path = ?", (str(tr.resolve()),))
    target._connection.commit()
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        assert comparison.target_scope.location.id is not None
        assert not comparison.target_scope.has_inventory


def test_same_database_roots_handles_and_copied_uuid(
    catalogs: tuple[Catalog, Catalog, Path, Path], tmp_path: Path
) -> None:
    source, _, sr, tr = catalogs
    _write(sr, "source", b"source")
    _write(tr, "target", b"target")
    source.scan_directory(sr)
    source.scan_directory(tr)
    with Catalog.open(source.database_path) as second:
        for target in (source, second):
            with source.compare_content(target, source_root=sr, target_root=sr) as comparison:
                assert comparison.summary() == ContentComparisonSummary(1, 1, 1, 0, 0, 0)
                assert list(comparison.batches()) == []
                assert len(comparison._connection.execute("PRAGMA database_list").fetchall()) == 2  # type: ignore[union-attr]
            with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
                assert comparison.summary().missing_source_content_count == 1
    copied_path = tmp_path / "copied.sqlite"
    shutil.copyfile(source.database_path, copied_path)
    with Catalog.open(copied_path) as copied:
        assert copied.catalog_uuid == source.catalog_uuid
        copied.apply_inventory_changes(copied.prepare_file_update(sr, remove_paths=[PurePosixPath("source")]))
        with source.compare_content(copied, source_root=sr, target_root=sr) as comparison:
            assert comparison.summary() == ContentComparisonSummary(1, 0, 0, 1, 0, 6)


def test_readonly_stale_files_and_encoded_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sr, tr = tmp_path / "source", tmp_path / "target"
    sr.mkdir()
    tr.mkdir()
    # '?' is not a valid Windows filename; '#', Unicode, and spaces are valid.
    special = "catalog # café" + ("?" if os.name != "nt" else "") + ".sqlite"
    with Catalog.create(tmp_path / special) as source, Catalog.create(tmp_path / "target.sqlite") as target:
        _write(sr, "file", b"source")
        _write(tr, "file", b"source")
        source.scan_directory(sr)
        target.scan_directory(tr)
        (tr / "file").unlink()
        _write(sr, "file", b"changed")
        before_files = ((sr / "file").read_bytes(), (sr / "file").stat())
        before_dumps = (_dump(source), _dump(target))
        before_bytes = (source.database_path.read_bytes(), target.database_path.read_bytes())

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("comparison must not scan, hash, or verify source files")

        monkeypatch.setattr(catalog_module, "regular_files", forbidden)
        monkeypatch.setattr(catalog_module, "hash_file_stably", forbidden)
        monkeypatch.setattr(Path, "open", forbidden)
        with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
            assert comparison.summary().missing_source_content_count == 0
            connection = comparison._require_connection()
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("DELETE FROM source_catalog.content")
            connection.execute("PRAGMA query_only = OFF")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("DELETE FROM target_catalog.content")
        monkeypatch.undo()
        assert (_dump(source), _dump(target)) == before_dumps
        assert (source.database_path.read_bytes(), target.database_path.read_bytes()) == before_bytes
        after = (sr / "file").stat()
        assert (sr / "file").read_bytes() == before_files[0]
        assert (after.st_mtime_ns, after.st_mode) == (before_files[1].st_mtime_ns, before_files[1].st_mode)


def test_lifetime_replay_overlap_and_pending_transactions(catalogs: tuple[Catalog, Catalog, Path, Path]) -> None:
    source, target, sr, tr = catalogs
    _write(sr, "one", b"one")
    source.scan_directory(sr)
    comparison = source.compare_content(target, source_root=sr, target_root=tr)
    with pytest.raises(ContentComparisonError):
        comparison.summary()
    with comparison:
        iterator = comparison.batches()
        with pytest.raises(ContentComparisonError, match="one batch"):
            comparison.batches()
        iterator.close()  # even before first next()
        assert comparison._cursor is None
        iterator = comparison.batches()
        assert next(iterator)[0].content_id == _identity(b"one")
        iterator.close()
        assert len(list(comparison.batches())) == 1
        abandoned = comparison.batches()
    assert list(abandoned) == []
    comparison.close()
    for method in (comparison.summary, comparison.batches, comparison.__enter__):
        with pytest.raises(ContentComparisonError):
            method()
    assert source.files(sr).count() == 1
    for catalog in (source, target):
        catalog._connection.execute("INSERT INTO tags(name) VALUES ('pending')")
        with pytest.raises(ContentComparisonError, match="transactions"):
            with source.compare_content(target, source_root=sr, target_root=tr):
                pass
        assert catalog._connection.in_transaction
        assert catalog._connection.execute("SELECT name FROM tags WHERE name = 'pending'").fetchone() is not None
        catalog._connection.rollback()


@pytest.mark.parametrize("value", [True, 1.5, "1", None])
def test_invalid_batch_types(catalogs: tuple[Catalog, Catalog, Path, Path], value: Any) -> None:
    source, target, sr, tr = catalogs
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        with pytest.raises(TypeError):
            comparison.batches(batch_size=value)
        with pytest.raises(TypeError):
            comparison.batches(include_present=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, 2**63])
def test_invalid_batch_ranges(catalogs: tuple[Catalog, Catalog, Path, Path], value: int) -> None:
    source, target, sr, tr = catalogs
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        with pytest.raises(ValueError):
            comparison.batches(batch_size=value)


def test_invalid_inputs(catalogs: tuple[Catalog, Catalog, Path, Path], tmp_path: Path) -> None:
    source, target, sr, tr = catalogs
    with pytest.raises(TypeError):
        source.compare_content(None, source_root=sr, target_root=tr)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        source.compare_content(target, source_root=str(sr), target_root=tr)  # type: ignore[arg-type]
    with pytest.raises(ContentComparisonError):
        source.compare_content(target, source_root=tmp_path / "absent", target_root=tr)
    closed = Catalog.open(target.database_path)
    closed.close()
    with pytest.raises(ContentComparisonError) as failure:
        with source.compare_content(closed, source_root=sr, target_root=tr):
            pass
    assert isinstance(failure.value.__cause__, sqlite3.Error)
    missing = tmp_path / "missing.sqlite"
    old_path = target._database_path
    target._database_path = missing
    try:
        with pytest.raises(ContentComparisonError):
            with source.compare_content(target, source_root=sr, target_root=tr):
                pass
        assert not missing.exists()
        with Catalog.create(tmp_path / "replacement.sqlite") as replacement:
            target._database_path = replacement.database_path
            with pytest.raises(ContentComparisonError, match="replaced"):
                with source.compare_content(target, source_root=sr, target_root=tr):
                    pass
    finally:
        target._database_path = old_path


@pytest.mark.parametrize(
    "corruption",
    [
        "orphan",
        "size",
        "digest",
        "algorithm",
        "pointer",
        "status",
        "location",
        "uuid",
        "origin",
        "schema",
        "version1",
        "version2",
        "version99",
    ],
)
@pytest.mark.parametrize("side", ["source", "target"])
def test_corrupt_scoped_inputs_fail_without_writes(
    catalogs: tuple[Catalog, Catalog, Path, Path], corruption: str, side: str
) -> None:
    source, target, sr, tr = catalogs
    _write(sr, "shared", b"shared")
    _write(tr, "shared", b"shared")
    _write(tr, "target-only", b"target-only")
    source.scan_directory(sr)
    target.scan_directory(tr)
    damaged = source if side == "source" else target
    connection = damaged._connection
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA ignore_check_constraints = ON")
    statements = {
        "orphan": "UPDATE file_observations SET content_id = 99999",
        "size": "UPDATE file_observations SET size_bytes = 99999",
        "digest": "UPDATE content SET digest = upper(digest)",
        "algorithm": "UPDATE content SET algorithm = 'sha512'",
        "pointer": "UPDATE locations SET current_scan_id = 99999",
        "status": "UPDATE scan_runs SET status = 'running'",
        "location": "UPDATE scan_runs SET location_id = 99999",
        "uuid": "UPDATE catalog_metadata SET catalog_uuid = 'invalid'",
        "origin": "UPDATE scan_runs SET origin = 'invalid'",
        "schema": "DROP TABLE tags",
        "version1": "UPDATE catalog_metadata SET schema_version = 1",
        "version2": "UPDATE catalog_metadata SET schema_version = 2",
        "version99": "UPDATE catalog_metadata SET schema_version = 99",
    }
    connection.execute(statements[corruption])
    connection.commit()
    before = (_dump(source), _dump(target), damaged.database_path.read_bytes())
    with pytest.raises(ContentComparisonError):
        with source.compare_content(target, source_root=sr, target_root=tr):
            pytest.fail("invalid records must be rejected before exposing the session")
    assert (_dump(source), _dump(target), damaged.database_path.read_bytes()) == before


def test_shared_size_contradiction(catalogs: tuple[Catalog, Catalog, Path, Path]) -> None:
    source, target, sr, tr = catalogs
    for catalog, root in ((source, sr), (target, tr)):
        _write(root, "shared", b"shared")
        catalog.scan_directory(root)
    target._connection.execute("UPDATE content SET size_bytes = 7")
    target._connection.execute("UPDATE file_observations SET size_bytes = 7")
    target._connection.commit()
    with pytest.raises(ContentComparisonError, match="contradictory"):
        with source.compare_content(target, source_root=sr, target_root=tr):
            pass


def test_invalid_target_only_record_and_missing_metadata(catalogs: tuple[Catalog, Catalog, Path, Path]) -> None:
    source, target, sr, tr = catalogs
    _write(sr, "shared", b"shared")
    _write(tr, "shared", b"shared")
    _write(tr, "target-only", b"other")
    source.scan_directory(sr)
    target.scan_directory(tr)
    target._connection.execute("UPDATE content SET digest = 'invalid' WHERE digest = ?", (_identity(b"other").digest,))
    target._connection.commit()
    with pytest.raises(ContentComparisonError, match="invalid scoped"):
        with source.compare_content(target, source_root=sr, target_root=tr):
            pass
    target._connection.execute("DELETE FROM catalog_metadata")
    target._connection.commit()
    with pytest.raises(ContentComparisonError, match="metadata is missing"):
        with source.compare_content(target, source_root=sr, target_root=tr):
            pass


def test_empty_source_and_populated_target(catalogs: tuple[Catalog, Catalog, Path, Path]) -> None:
    source, target, sr, tr = catalogs
    _write(tr, "only", b"only")
    target.scan_directory(tr)
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        assert comparison.summary() == ContentComparisonSummary(0, 1, 0, 0, 1, 0)
        assert list(comparison.batches(include_present=True)) == []


def test_opening_failure_closes_dedicated_connection(
    catalogs: tuple[Catalog, Catalog, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, sr, tr = catalogs
    connection = sqlite3.connect(":memory:", uri=True)
    monkeypatch.setattr(comparison_module, "_connect", lambda: connection)
    target._connection.execute("UPDATE catalog_metadata SET schema_version = 99")
    target._connection.commit()
    with pytest.raises(ContentComparisonError):
        with source.compare_content(target, source_root=sr, target_root=tr):
            pass
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    assert source.schema_version == 4 and target.schema_version == 99


def test_wal_publication_and_pruning_preserve_both_snapshots(catalogs: tuple[Catalog, Catalog, Path, Path]) -> None:
    source, target, sr, tr = catalogs
    for catalog, root in ((source, sr), (target, tr)):
        catalog._connection.execute("PRAGMA journal_mode = WAL")
        _write(root, "old", b"old")
        catalog.scan_directory(root)
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        summary = comparison.summary()
        iterator = comparison.batches(batch_size=1, include_present=True)
        assert next(iterator)[0].in_target
        with Catalog.open(source.database_path) as sw, Catalog.open(target.database_path) as tw:
            _write(sr, "new", b"new")
            sw.apply_inventory_changes(sw.prepare_file_update(sr, paths=Path("new")), keep_last=1)
            tw.apply_inventory_changes(tw.prepare_file_update(tr, remove_paths=[PurePosixPath("old")]), keep_last=1)
        assert comparison.summary() == summary
        assert list(iterator) == []
        rows = [row for batch in comparison.batches(include_present=True) for row in batch]
        assert len(rows) == 1 and rows[0].in_target
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        assert comparison.summary() == ContentComparisonSummary(2, 0, 0, 2, 0, 6)


def test_rollback_contention_releases_on_close(catalogs: tuple[Catalog, Catalog, Path, Path]) -> None:
    source, target, sr, tr = catalogs
    source.scan_directory(sr)
    with source.compare_content(target, source_root=sr, target_root=tr):
        with Catalog.open(source.database_path) as writer:
            writer._connection.execute("PRAGMA busy_timeout = 0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with writer._connection:
                    writer._connection.execute("INSERT INTO tags(name) VALUES ('blocked')")
            assert not writer._connection.in_transaction
    with source._connection:
        source._connection.execute("INSERT INTO tags(name) VALUES ('unblocked')")


class _BoundedCursor(sqlite3.Cursor):
    fail_after: int | None = None
    calls = 0

    def fetchall(self) -> list[Any]:
        raise AssertionError("unbounded fetch")

    def fetchmany(self, size: int | None = None) -> list[Any]:
        assert size is not None and size > 0
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise sqlite3.OperationalError("injected batch read failure")
        return super().fetchmany(size)


class _BoundedConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        return self.cursor(factory=_BoundedCursor).execute(sql, parameters)


def test_large_bounded_batches_and_query_plan(
    catalogs: tuple[Catalog, Catalog, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, sr, tr = catalogs
    # Seed a large valid snapshot directly, avoiding filesystem test setup cost.
    for catalog, root in ((source, sr), (target, tr)):
        catalog.scan_directory(root)
        with catalog._connection:
            for index in range(120):
                identity = _identity(str(index).encode())
                cursor = catalog._connection.execute(
                    "INSERT INTO content(algorithm, digest, size_bytes) VALUES (?, ?, 1)",
                    (identity.algorithm, identity.digest),
                )
                content_id = cursor.lastrowid
                count = 1500 if index == 0 else 1
                catalog._connection.executemany(
                    "INSERT INTO file_observations(scan_id, relative_path, content_id, size_bytes, mtime_ns) "
                    "VALUES (1, ?, ?, 1, 0)",
                    ((f"{index}/{member}", content_id) for member in range(count)),
                )
    queries: list[str] = []

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:", uri=True, factory=_BoundedConnection)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(comparison_module, "_connect", connect)
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        assert comparison.summary() == ContentComparisonSummary(120, 120, 120, 0, 0, 0)
        queries.clear()
        rows: list[ComparedContent] = []
        for batch in comparison.batches(batch_size=7, include_present=True):
            assert isinstance(batch, tuple) and len(batch) <= 7
            rows.extend(batch)
        assert len(rows) == len({row.content_id for row in rows}) == 120
        assert max(row.source_instance_count for row in rows) == 1500
        assert len(queries) == 1  # one SQL traversal, no per-identity target query
        with closing(
            comparison._require_connection().execute(
                "EXPLAIN QUERY PLAN " + comparison._relations + "SELECT * FROM classified", comparison._parameters
            )
        ) as cursor:
            plan = [str(row[3]) for row in cursor]
        assert any("INDEX" in detail and "scan_id" in detail for detail in plan)
        # Maximum accepted size does not preallocate an enormous batch.
        assert len(next(comparison.batches(batch_size=2**63 - 1, include_present=True))) == 120


@pytest.mark.parametrize("interrupt", [False, True])
def test_read_failure_and_interruption_release_resources(
    catalogs: tuple[Catalog, Catalog, Path, Path], monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    source, target, sr, tr = catalogs
    _write(sr, "one", b"one")
    _write(sr, "two", b"two")
    source.scan_directory(sr)

    class FailingCursor(_BoundedCursor):
        def fetchmany(self, size: int | None = None) -> list[Any]:
            if self.calls and interrupt:
                raise KeyboardInterrupt
            self.fail_after = 1
            return super().fetchmany(size)

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            return self.cursor(factory=FailingCursor).execute(sql, parameters)

    monkeypatch.setattr(
        comparison_module, "_connect", lambda: sqlite3.connect(":memory:", uri=True, factory=FailingConnection)
    )
    with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
        iterator = comparison.batches(batch_size=1)
        next(iterator)
        with pytest.raises(KeyboardInterrupt if interrupt else ContentComparisonError):
            next(iterator)
        assert comparison._connection is None and comparison._cursor is None
        with pytest.raises(ContentComparisonError):
            comparison.summary()
    assert source.files(sr).count() == 2


@pytest.mark.parametrize("interrupt", [False, True])
def test_scratch_sink_is_ready_only_after_exhaustion(
    catalogs: tuple[Catalog, Catalog, Path, Path], interrupt: bool
) -> None:
    source, target, sr, tr = catalogs
    for index in range(7):
        _write(sr, str(index), str(index).encode())
    _write(tr, "shared", b"0")
    source.scan_directory(sr)
    target.scan_directory(tr)
    before = (_dump(source), _dump(target))
    with closing(sqlite3.connect(":memory:")) as sink:
        sink.executescript(
            "CREATE TABLE entries(digest TEXT, in_target INTEGER); "
            "CREATE TABLE build(source_uuid TEXT, target_uuid TEXT, expected INTEGER, ready INTEGER)"
        )
        try:
            with source.compare_content(target, source_root=sr, target_root=tr) as comparison:
                sink.execute(
                    "INSERT INTO build VALUES (?, ?, ?, 0)",
                    (
                        comparison.source_scope.catalog_uuid,
                        comparison.target_scope.catalog_uuid,
                        comparison.summary().source_content_count,
                    ),
                )
                for batch in comparison.batches(batch_size=2, include_present=True):
                    sink.executemany(
                        "INSERT INTO entries VALUES (?, ?)", ((row.content_id.digest, row.in_target) for row in batch)
                    )
                    if interrupt:
                        raise RuntimeError("interrupted sink")
                sink.execute("UPDATE build SET ready = 1")
        except RuntimeError:
            pass
        assert sink.execute("SELECT ready FROM build").fetchone()[0] == int(not interrupt)
        if not interrupt:
            assert sink.execute("SELECT COUNT(*), SUM(in_target) FROM entries").fetchone() == (7, 1)
    assert (_dump(source), _dump(target)) == before
