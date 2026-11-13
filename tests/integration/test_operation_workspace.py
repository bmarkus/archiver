from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

import pytest

import archiver.catalog as catalog_module
from archiver import (
    Catalog,
    ContentId,
    InvalidCatalogError,
    InventoryChanges,
    ManagedRootAuthority,
    ManifestEntry,
    OperationCounts,
    OperationError,
    OperationSpec,
    PlacementCheck,
    PlacementEvidence,
    RefreshFailure,
    StaleRefreshError,
    SyncWorkspace,
    TagProvenance,
)
from archiver.models import FileObservation, Location
from archiver.operations import prepare_operation_request, recover_placement


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[Catalog, Catalog, OperationSpec, Path]]:
    a, b, parent = tmp_path / "source", tmp_path / "target", tmp_path / "workspaces"
    for path in (a, b, parent):
        path.mkdir()
    with Catalog.create(tmp_path / "s.sqlite") as source, Catalog.create(tmp_path / "t.sqlite") as target:
        spec = OperationSpec.sync(source.operation_scope(a), target.operation_scope(b))
        yield source, target, spec, parent


def identity(data: bytes) -> ContentId:
    return ContentId("sha256", hashlib.sha256(data).hexdigest())


def entry(data: bytes = b"abc", name: str = "nested/a") -> ManifestEntry:
    return ManifestEntry(identity(data), len(data), PurePosixPath(name), 17, PurePosixPath(name))


def evidence(spec: OperationSpec, item: ManifestEntry) -> PlacementEvidence:
    assert item.destination_relative_path is not None
    staging = f".archiver-{spec.operation_id}"
    return PlacementEvidence(
        staging,
        staging + f"/{item.content_id.digest}.copy",
        item.destination_relative_path.as_posix(),
        1,
        42,
        item.content_id,
        item.size_bytes,
        21,
    )


def check(e: PlacementEvidence) -> PlacementCheck:
    return PlacementCheck(True, e.device, e.file_id, True, True, e.content_id, e.size_bytes, e.mtime_ns)


def placed(w: SyncWorkspace, item: ManifestEntry) -> tuple[int, PlacementEvidence]:
    found = next(e for batch in w.batches() for e in batch if e.content_id == item.content_id)
    assert found.entry_id is not None
    e = evidence(w.spec, item)
    w.record_outcome(found.entry_id, "copying")
    w.record_outcome(found.entry_id, "verified", evidence=e)
    w.record_outcome(found.entry_id, "placed")
    return found.entry_id, e


def changes(spec: OperationSpec, items: tuple[ManifestEntry, ...]) -> InventoryChanges:
    location = Location(None, spec.target.root)
    return InventoryChanges(
        location,
        spec.target.revision,
        tuple(
            FileObservation(location, i.destination_relative_path, i.content_id, i.size_bytes, 21)
            for i in items
            if i.destination_relative_path is not None
        ),
        (),
    )


def test_workspace_storage_readonly_reopen_bounded_and_source_safety(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
) -> None:
    source, target, spec, parent = setup
    assert spec.source is not None
    file = spec.source.root / "untouched"
    file.write_bytes(b"unchanged")
    before = file.stat()
    source_dump = tuple(source._connection.iterdump())
    target_dump = tuple(target._connection.iterdump())
    items = tuple(entry(str(n).encode(), f"d/{n}") for n in range(113)) + (entry(b"", "empty"),)
    w = SyncWorkspace.create(parent, spec, expected_count=len(items), expected_bytes=sum(i.size_bytes for i in items))
    directory = w.directory
    with pytest.raises(ValueError, match="maximum"):
        w.add(items, batch_size=10)
    assert w.summary().planned == 0
    for n in range(0, len(items), 13):
        w.add(items[n : n + 13], batch_size=13)
    batches = tuple(w.batches(batch_size=7))
    assert all(len(b) <= 7 for b in batches)
    ids = [i.content_id.digest for b in batches for i in b]
    assert ids == sorted(ids) and len(set(ids)) == len(items)
    assert w.summary().planned == len(items)
    w.mark_ready(comparison_exhausted=True)
    with SyncWorkspace.open(directory) as reader:
        assert reader.spec == spec and reader.state == "ready"
        with pytest.raises(OperationError, match="controller"):
            reader.fail_before_execution()
    with pytest.raises(OperationError, match="controller"):
        SyncWorkspace.open(directory, writable=True)
    w.close()
    assert directory.exists()
    with SyncWorkspace.open(directory, writable=True) as reopened:
        assert reopened.summary().planned == len(items)
        reopened.dispose(target, discard=True)
    assert not directory.exists()
    assert tuple(source._connection.iterdump()) == source_dump
    assert tuple(target._connection.iterdump()) == target_dump
    assert file.read_bytes() == b"unchanged"
    assert (file.stat().st_mtime_ns, file.stat().st_mode) == (before.st_mtime_ns, before.st_mode)


def test_build_failure_transaction_unresolved_paths_and_interrupted_build(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
) -> None:
    _, target, spec, parent = setup
    with SyncWorkspace.create(parent, spec, expected_count=1, expected_bytes=3) as w:
        with pytest.raises(sqlite3.IntegrityError):
            w.add((entry(), entry()))
        assert w.summary().planned == 0
        with pytest.raises(OperationError):
            w.mark_ready(comparison_exhausted=False)
        w.add((replace(entry(), destination_relative_path=None),))
        with pytest.raises(OperationError, match="unresolved"):
            w.mark_ready(comparison_exhausted=True)
        directory = w.directory
    with SyncWorkspace.open(directory, writable=True) as reopened:
        with pytest.raises(OperationError, match="totals"):
            reopened.mark_ready(comparison_exhausted=True)
        with pytest.raises(OperationError, match="uninterrupted"):
            reopened.add(())
        reopened.dispose(target, discard=True)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a\\b", "a:b"])
def test_invalid_manifest_paths(name: str) -> None:
    with pytest.raises(ValueError):
        entry(name=name)


def test_authority_scope_control_artifacts_and_collision_safety(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
) -> None:
    source, target, spec, parent = setup
    assert spec.source is not None
    with pytest.raises(ValueError, match="outside"):
        SyncWorkspace.create(spec.target.root, spec, expected_count=0, expected_bytes=0)
    with pytest.raises(ValueError, match="disjoint"):
        OperationSpec.sync(spec.target, spec.target)
    with SyncWorkspace.create(parent, spec, expected_count=2, expected_bytes=4) as w:
        with pytest.raises(OperationError, match="control"):
            w.add((entry(name=f".archiver-{spec.operation_id}/witness"),))
        w.add((entry(b"a", "d"),))
        with pytest.raises(OperationError, match="ancestor"):
            w.add((entry(b"b", "d/nested"),))
        w.add((entry(b"abc", "other"),))
        w.mark_ready(comparison_exhausted=True)
        with pytest.raises(OperationError, match="authority"):
            w.begin(source, target, authority=ManagedRootAuthority(str(uuid4()), spec.target.root, "sync"))
        assert target.operations() == ()
        (spec.target.root / "unobserved").write_bytes(b"new")
        target.scan_directory(spec.target.root)
        with pytest.raises(OperationError, match="baseline"):
            w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
        assert w.state == "ready" and target.operations() == ()


def test_frozen_receipt_retry_pruning_and_lost_ack(setup: tuple[Catalog, Catalog, OperationSpec, Path]) -> None:
    source, target, spec, parent = setup
    items = (entry(), entry(b"", "empty"))
    w = SyncWorkspace.create(parent, spec, expected_count=2, expected_bytes=3)
    w.add(items)
    w.mark_ready(comparison_exhausted=True)
    w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
    for item in items:
        placed(w, item)
    request = w.freeze()
    prepared = changes(spec, items)
    assert prepare_operation_request(spec, request.counts, prepared) == request
    receipt = target._finalize_operation(request, prepared)
    assert receipt.status == "completed" and receipt.counts.registered == 2
    assert w.state == "finalizing"  # acknowledgement deliberately lost
    revision = receipt.committed_revision
    target.scan_directory(spec.target.root, keep_last=1)
    assert target._finalize_operation(request, prepared) == receipt
    assert target.operation_scope(spec.target.root).revision != revision
    with pytest.raises(OperationError, match="different request"):
        modified = replace(request.counts, planned_bytes=99)
        target._finalize_operation(prepare_operation_request(spec, modified, prepared), prepared)
    directory = w.directory
    w.close()
    with SyncWorkspace.open(directory, writable=True) as reopened:
        assert reopened.state == "recovery_required"
        assert reopened.acknowledge(target) == receipt
        assert reopened.state == "completed"
        assert reopened.summary().registered == 2
        with pytest.raises(OperationError, match="artifacts"):
            reopened.dispose(target)
        reopened.dispose(target, artifacts_resolved=True)
    assert not directory.exists()
    assert len(target.operations()) == 1
    assert source.operations() == ()


@pytest.mark.parametrize(
    "outcome,expected", [("failed", "failed"), ("conflict", "failed"), ("pending", "failed"), ("skipped", "completed")]
)
def test_zero_success_receipts_and_cleanup(
    setup: tuple[Catalog, Catalog, OperationSpec, Path], outcome: str, expected: str
) -> None:
    source, target, spec, parent = setup
    item = entry()
    if outcome == "skipped":
        item = replace(item, state="skipped", action="skip_present", destination_relative_path=None)
    with SyncWorkspace.create(parent, spec, expected_count=1, expected_bytes=3) as w:
        w.add((item,))
        w.mark_ready(comparison_exhausted=True)
        w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
        if outcome in ("failed", "conflict"):
            w.record_outcome(1, outcome, error_code="injected")
        request = w.freeze()
        result = target._finalize_operation(request, changes(spec, ()))
        assert result.status == expected
        assert result.committed_revision == spec.target.revision
        assert target._connection.execute("SELECT count(*) FROM scan_runs").fetchone()[0] == 0
        directory = w.directory
        w.dispose(target)
        assert not directory.exists()


@pytest.mark.parametrize("stage", ["insert", "pointer", "receipt", "prune"])
def test_publication_failure_rolls_back_inventory_receipt_and_retention(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    source, target, original, parent = setup
    target.scan_directory(original.target.root)
    spec = replace(original, target=target.operation_scope(original.target.root), keep_last=1)
    with SyncWorkspace.create(parent, spec, expected_count=1, expected_bytes=3) as w:
        w.add((entry(),))
        w.mark_ready(comparison_exhausted=True)
        w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
        placed(w, entry())
        request = w.freeze()
        before = tuple(target._connection.iterdump())
        method = {
            "insert": "_get_or_create_content",
            "pointer": "_complete_scan",
            "receipt": "_write_operation_receipt",
            "prune": "_delete_history",
        }[stage]
        original_method = getattr(target, method)

        def fail(*args: object, **kwargs: object) -> None:
            original_method(*args, **kwargs)
            raise RuntimeError("injected after state change")

        monkeypatch.setattr(target, method, fail)
        with pytest.raises(RuntimeError, match="injected"):
            target._finalize_operation(request, changes(spec, (entry(),)))
        assert tuple(target._connection.iterdump()) == before
        w.require_recovery()
        with pytest.raises(OperationError, match="no confirmed"):
            w.dispose(target)
        assert w.directory.exists()
        monkeypatch.setattr(target, method, original_method)
        receipt = target._finalize_operation(request, changes(spec, (entry(),)))
        assert receipt.status == "completed"


def test_recovery_checks_frozen_immutability_and_fencing(setup: tuple[Catalog, Catalog, OperationSpec, Path]) -> None:
    source, target, spec, parent = setup
    w = SyncWorkspace.create(parent, spec, expected_count=1, expected_bytes=3)
    w.add((entry(),))
    w.mark_ready(comparison_exhausted=True)
    authority = ManagedRootAuthority(spec.operation_id, spec.target.root, "sync")
    w.begin(source, target, authority=authority)
    entry_id, e = placed(w, entry())
    frozen = w.freeze()
    w.require_recovery()
    with pytest.raises(OperationError, match="missing"):
        w.resume(source, target, authority=authority, checks=())
    with pytest.raises(OperationError, match="invalidated"):
        w.resume(source, target, authority=authority, checks=((entry_id, replace(check(e), destination_mtime_ns=88)),))
    assert w.frozen_request == frozen
    w.resume(source, target, authority=authority, checks=((entry_id, check(e)),))
    assert w.state == "finalizing"
    w.require_recovery()
    w.fence(target, superseded=True)
    assert target.operation(spec.operation_id).status == "superseded"  # type: ignore[union-attr]
    with pytest.raises(OperationError, match="fenced"):
        target._finalize_operation(frozen, changes(spec, (entry(),)))
    new_spec = replace(spec, operation_id=str(uuid4()), predecessor_id=spec.operation_id)
    with SyncWorkspace.create(parent, new_spec, expected_count=1, expected_bytes=3) as successor:
        w.handoff(successor, (entry_id,))
        w.handoff(successor, (entry_id,))  # retry after lost handoff acknowledgement
        transferred = next(successor.batches())[0]
        assert transferred.action == "register_existing_verified"
        assert transferred.destination_relative_path == entry().destination_relative_path
        assert transferred.evidence == e and transferred.predecessor_entry == entry_id
        successor.dispose(target, discard=True, artifacts_resolved=True)
    w.dispose(target, discard=True, artifacts_resolved=True)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({"witness_regular": False}, "reconciliation_required"),
        ({"witness_file_id": 999}, "reconciliation_required"),
        ({"same_file": False}, "conflict"),
        ({"destination_exists": False}, "pending"),
        ({"destination_content": identity(b"other")}, "reconciliation_required"),
        ({}, "placed"),
    ],
)
def test_retained_witness_decisions(
    setup: tuple[Catalog, Catalog, OperationSpec, Path], mutation: dict[str, object], expected: str
) -> None:
    _, _, spec, _ = setup
    e = evidence(spec, entry())
    actual = replace(check(e), **mutation)  # type: ignore[arg-type]
    assert recover_placement(e, actual, frozen=False) == expected
    if expected in ("pending", "conflict"):
        assert recover_placement(e, actual, frozen=True) == "reconciliation_required"


def test_unknown_formats_and_unsafe_disposal(setup: tuple[Catalog, Catalog, OperationSpec, Path]) -> None:
    _, target, spec, parent = setup
    w = SyncWorkspace.create(parent, spec, expected_count=0, expected_bytes=0)
    directory = w.directory
    w.close()
    with closing(sqlite3.connect(directory / "workspace.sqlite3")) as connection, connection:
        connection.execute("UPDATE header SET format=99")
    before = (directory / "workspace.sqlite3").read_bytes()
    with pytest.raises(OperationError, match="format"):
        SyncWorkspace.open(directory, writable=True)
    assert (directory / "workspace.sqlite3").read_bytes() == before
    with closing(sqlite3.connect(directory / "workspace.sqlite3")) as connection, connection:
        connection.execute("UPDATE header SET format=1")
    with SyncWorkspace.open(directory, writable=True) as w:
        unknown = directory / "user-data"
        unknown.write_bytes(b"must remain")
        with pytest.raises(OperationError, match="unknown"):
            w.dispose(target, discard=True)
        assert unknown.read_bytes() == b"must remain"
        unknown.unlink()
        w.dispose(target, discard=True)


def test_schema3_explicit_migration_preserves_data_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_bytes(b"abc")
    database = tmp_path / "old.sqlite"
    with Catalog.create(database) as catalog:
        catalog.scan_directory(root)
        provenance = TagProvenance("user", "migration-test", "1")
        catalog.add_content_tag(identity(b"abc"), "kept", provenance)
        catalog.add_content_tag(identity(b"abc"), "retracted", provenance)
        catalog.retract_content_tag(identity(b"abc"), "retracted")
        metadata = tuple(catalog._connection.execute("SELECT * FROM content_tag_assertions"))
        uuid = catalog.catalog_uuid
        revision = catalog.operation_scope(root).revision
        catalog._connection.execute("DROP TABLE operations")
        catalog._connection.execute("UPDATE catalog_metadata SET schema_version=3")
        catalog._connection.commit()
        dump = tuple(catalog._connection.iterdump())
    before = database.read_bytes()
    with pytest.raises(InvalidCatalogError, match="migrate"):
        Catalog.open(database)
    assert database.read_bytes() == before
    original = getattr(catalog_module, "OPERATION_SCHEMA")
    monkeypatch.setattr(catalog_module, "OPERATION_SCHEMA", original + ";SELECT missing FROM missing_table;")
    with pytest.raises(InvalidCatalogError):
        Catalog.migrate(database)
    with closing(sqlite3.connect(database)) as connection:
        assert tuple(connection.iterdump()) == dump
    monkeypatch.setattr(catalog_module, "OPERATION_SCHEMA", original)
    assert Catalog.migrate(database) is True
    assert Catalog.migrate(database) is False
    with Catalog.open(database) as catalog:
        assert catalog.catalog_uuid == uuid and catalog.schema_version == 4
        assert catalog.operation_scope(root).revision == revision
        assert catalog._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert len(catalog.files(root).fetch()) == 1
        assert [tuple(row) for row in catalog._connection.execute("SELECT * FROM content_tag_assertions")] == [
            tuple(row) for row in metadata
        ]


def test_takeover_and_committed_intent_before_verification(setup: tuple[Catalog, Catalog, OperationSpec, Path]) -> None:
    source, target, spec, parent = setup
    w = SyncWorkspace.create(parent, spec, expected_count=1, expected_bytes=3)
    w.add((entry(),))
    w.mark_ready(comparison_exhausted=True)
    w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
    w.record_outcome(1, "copying")
    stored = next(w.batches())[0]
    assert stored.evidence is None and stored.placement_intent is not None
    assert stored.placement_intent.witness_path.endswith(entry().content_id.digest + ".copy")
    with SyncWorkspace.open(w.directory) as inspector:
        assert inspector.state == "executing"  # inspection never changes persistent state
    # Simulate a crash: close the connection, retain the controller artifact.
    w._connection.close()
    w._closed = True
    with pytest.raises(OperationError, match="controller"):
        SyncWorkspace.open(w.directory, writable=True)
    with SyncWorkspace.open(w.directory, writable=True, takeover=True) as recovered:
        assert recovered.state == "recovery_required"
        with pytest.raises(OperationError, match="missing"):
            recovered.resume(
                source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"), checks=()
            )
        recovered.dispose(target, discard=True, artifacts_resolved=True)


def test_stale_finalization_and_existing_transaction_leave_state_unchanged(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
) -> None:
    _, target, spec, _ = setup
    target._start_operation(spec)
    prepared = changes(spec, (entry(),))
    counts = OperationCounts(planned=1, planned_bytes=3, placed=1, placed_bytes=3, registered=1, registered_bytes=3)
    request = prepare_operation_request(spec, counts, prepared)
    target._connection.execute("BEGIN")
    target._connection.execute("INSERT INTO content(algorithm,digest,size_bytes) VALUES('sha256',?,0)", ("f" * 64,))
    with pytest.raises(RefreshFailure, match="existing transaction"):
        target._finalize_operation(request, prepared)
    assert target._connection.in_transaction
    target._connection.rollback()
    target.scan_directory(spec.target.root)
    before = tuple(target._connection.iterdump())
    with pytest.raises(StaleRefreshError):
        target._finalize_operation(request, prepared)
    assert tuple(target._connection.iterdump()) == before


def test_shared_catalog_roots_and_local_dedup_receipt(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "one").write_bytes(b"same")
    (a / "two").write_bytes(b"same")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(a)
        source_scope = catalog.operation_scope(a)
        spec = OperationSpec.sync(source_scope, catalog.operation_scope(b))
        with SyncWorkspace.create(tmp_path, spec, expected_count=0, expected_bytes=0) as w:
            w.mark_ready(comparison_exhausted=True)
            w.begin(catalog, catalog, authority=ManagedRootAuthority(spec.operation_id, b, "sync"))
            request = w.freeze()
            catalog._finalize_operation(request, changes(spec, ()))
            w.dispose(catalog)
        assert catalog.operation_scope(a) == source_scope
        local = OperationSpec(str(uuid4()), "dedup", source_scope)
        prepared = catalog.prepare_file_update(a, remove_paths=(PurePosixPath("two"),))
        # Database-only simulation: physical deletion remains 005.5 and both files remain.
        catalog._start_operation(local)
        counts = OperationCounts(planned=1, placed=1, registered=1)
        result = catalog._finalize_operation(prepare_operation_request(local, counts, prepared), prepared)
        assert result.status == "completed"
        assert (a / "one").read_bytes() == (a / "two").read_bytes() == b"same"
        assert len(catalog.files(a).fetch()) == 1


def test_cleanup_failure_retains_committed_receipt_and_allows_retry(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, spec, parent = setup
    w = SyncWorkspace.create(parent, spec, expected_count=0, expected_bytes=0)
    w.mark_ready(comparison_exhausted=True)
    w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
    request = w.freeze()
    receipt = target._finalize_operation(request, changes(spec, ()))
    directory = w.directory
    original = Path.unlink

    def fail(path: Path, missing_ok: bool = False) -> None:
        if path == directory / "workspace.sqlite3":
            raise PermissionError("injected locked database")
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(PermissionError, match="locked"):
        w.dispose(target)
    assert directory.exists() and target.operation(spec.operation_id) == receipt
    monkeypatch.setattr(Path, "unlink", original)
    with SyncWorkspace.open(directory, writable=True) as reopened:
        reopened.dispose(target)
    assert target.operation(spec.operation_id) == receipt


def test_receipt_acknowledgement_does_not_require_source_online(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
) -> None:
    source, target, spec, parent = setup
    assert spec.source is not None
    w = SyncWorkspace.create(parent, spec, expected_count=0, expected_bytes=0)
    w.mark_ready(comparison_exhausted=True)
    w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
    request = w.freeze()
    receipt = target._finalize_operation(request, changes(spec, ()))
    directory = w.directory
    w.close()
    offline = spec.source.root.with_name("offline")
    spec.source.root.rename(offline)
    with SyncWorkspace.open(directory, writable=True) as reopened:
        assert reopened.acknowledge(target) == receipt
        reopened.dispose(target)
    assert offline.is_dir()


@pytest.mark.parametrize("damage", ["table", "digest", "reservation", "frozen"])
def test_corrupt_workspace_rejected_before_writable_takeover(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
    damage: str,
) -> None:
    source, target, spec, parent = setup
    w = SyncWorkspace.create(parent, spec, expected_count=1, expected_bytes=3)
    w.add((entry(),))
    w.mark_ready(comparison_exhausted=True)
    w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
    placed(w, entry())
    w.freeze()
    directory = w.directory
    w.close()
    with closing(sqlite3.connect(directory / "workspace.sqlite3")) as connection, connection:
        sql = {
            "table": "DROP TABLE handoffs",
            "digest": "UPDATE entries SET digest='invalid'",
            "reservation": "UPDATE entries SET destination_key='different'",
            "frozen": "UPDATE frozen_entries SET row='[]'",
        }[damage]
        connection.execute(sql)
    before = (directory / "workspace.sqlite3").read_bytes()
    with pytest.raises((OperationError, ValueError, sqlite3.Error)):
        SyncWorkspace.open(directory, writable=True)
    assert (directory / "workspace.sqlite3").read_bytes() == before
    assert not (directory / "controller.json").exists()


def test_symlink_ancestors_rejected_before_source_resolution(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
) -> None:
    source, _, spec, parent = setup
    assert spec.source is not None
    alias = parent / "alias"
    try:
        alias.symlink_to(spec.source.root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(ValueError, match="symlinks"):
        source.operation_scope(alias)
    nested = spec.source.root / "alias"
    nested.symlink_to(spec.target.root, target_is_directory=True)
    with SyncWorkspace.create(parent, spec, expected_count=1, expected_bytes=3) as w:
        with pytest.raises(ValueError, match="symbolic"):
            w.add((entry(name="alias/file"),))
        assert w.summary().planned == 0


def test_partial_result_and_empty_request_accounting(setup: tuple[Catalog, Catalog, OperationSpec, Path]) -> None:
    source, target, spec, parent = setup
    with SyncWorkspace.create(parent, spec, expected_count=2, expected_bytes=4) as w:
        w.add((entry(), entry(b"x", "failed")))
        w.mark_ready(comparison_exhausted=True)
        w.begin(source, target, authority=ManagedRootAuthority(spec.operation_id, spec.target.root, "sync"))
        placed(w, entry())
        row = next(i for batch in w.batches() for i in batch if i.content_id == identity(b"x"))
        assert row.entry_id is not None
        w.record_outcome(row.entry_id, "failed", error_code="injected")
        request = w.freeze()
        record = target._finalize_operation(request, changes(spec, (entry(),)))
        assert record.status == "partial" and record.counts.failed == 1 and record.counts.registered == 1
        w.acknowledge(target)
        assert w.summary() == record.counts
        w.dispose(target, artifacts_resolved=True)


def test_control_database_exclusion_checks_both_endpoints(tmp_path: Path) -> None:
    source_root, target_root = tmp_path / "source", tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    with Catalog.create(target_root / "source.sqlite") as source, Catalog.create(tmp_path / "target.sqlite") as target:
        spec = OperationSpec.sync(source.operation_scope(source_root), target.operation_scope(target_root))
        with SyncWorkspace.create(tmp_path, spec, expected_count=1, expected_bytes=3) as w:
            for name in ("source.sqlite", "source.sqlite-wal", "source.sqlite-shm", "source.sqlite-journal"):
                with pytest.raises(OperationError, match="control"):
                    w.add((entry(name=name),))
            w.dispose(target, discard=True)


def test_unregistered_physical_success_cannot_be_terminal(setup: tuple[Catalog, Catalog, OperationSpec, Path]) -> None:
    _, _, spec, _ = setup
    counts = OperationCounts(planned=1, planned_bytes=3, placed=1, placed_bytes=3)
    with pytest.raises(OperationError, match="omitted"):
        prepare_operation_request(spec, counts, changes(spec, ()))


def test_existing_caller_transaction_and_receipt_wins_fence(
    setup: tuple[Catalog, Catalog, OperationSpec, Path],
) -> None:
    _, target, spec, _ = setup
    target._connection.execute("BEGIN")
    target._connection.execute("INSERT INTO content(algorithm,digest,size_bytes) VALUES('sha256',?,0)", ("0" * 64,))
    with pytest.raises(OperationError, match="caller"):
        target._start_operation(spec)
    assert target._connection.in_transaction
    target._connection.rollback()
    target._start_operation(spec)
    request = prepare_operation_request(spec, OperationCounts(), changes(spec, ()))
    receipt = target._finalize_operation(request, changes(spec, ()))
    assert target._fence_operation(spec, status="discarded") == receipt
    with Catalog.open(target.database_path) as reopened:
        assert reopened.operation(spec.operation_id) == receipt
        assert reopened.operations(limit=1) == (receipt,)
    for bound in (True, 0, -1, 2**63):
        with pytest.raises(ValueError):
            target.operations(limit=bound)
