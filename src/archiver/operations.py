"""Operation records and recovery decisions; no content-file execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from .models import ContentId, InventoryChanges, InventoryRevision

OperationKind = Literal["sync", "dedup"]
TerminalStatus = Literal["completed", "partial", "failed"]

OPERATION_SCHEMA = """
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('executing','completed','partial','failed','superseded','discarded')),
    started_at_ns INTEGER NOT NULL,
    completed_at_ns INTEGER,
    counts TEXT NOT NULL,
    request_digest TEXT,
    committed_revision TEXT,
    CHECK ((request_digest IS NULL AND committed_revision IS NULL)
        OR (request_digest IS NOT NULL AND committed_revision IS NOT NULL
            AND status IN ('completed','partial','failed')))
);
CREATE INDEX operations_started ON operations(started_at_ns, operation_id);
"""


class OperationError(Exception):
    """Invalid operation state, scope, evidence, or recovery request."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def positive(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError("batch/limit must be a positive SQLite integer")
    return value


def safe_root(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("roots must be Path values")
    if ".." in path.parts:
        raise ValueError("parent traversal is not allowed")
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("roots must not traverse symlinks or junctions")
    result = path.resolve(strict=True)
    if not result.is_dir():
        raise ValueError("root must be a directory")
    return result


def relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or "\0" in value
        or path.as_posix() != value
    ):
        raise ValueError("expected a canonical, nonescaping POSIX relative file path")
    return value


def revision_data(revision: InventoryRevision) -> list[object]:
    return [1, revision._catalog_uuid, str(revision._root), revision._snapshot_id]


def decode_revision(value: object) -> InventoryRevision:
    if not isinstance(value, list) or len(value) != 4 or type(value[0]) is not int or value[0] != 1:
        raise OperationError("unsupported revision encoding")
    _, catalog_uuid, root, snapshot = value
    if not isinstance(catalog_uuid, str) or not isinstance(root, str) or not Path(root).is_absolute():
        raise OperationError("invalid revision scope")
    UUID(catalog_uuid)
    if snapshot is not None and (type(snapshot) is not int or snapshot < 1):
        raise OperationError("invalid revision snapshot")
    return InventoryRevision(catalog_uuid, Path(root), snapshot)


@dataclass(frozen=True, slots=True)
class OperationScope:
    """Captured endpoint; database path is reopening evidence, not content identity."""

    database_path: Path
    revision: InventoryRevision

    def __post_init__(self) -> None:
        UUID(self.revision._catalog_uuid)
        if (
            not self.root.is_absolute()
            or ".." in self.root.parts
            or not self.database_path.is_absolute()
            or ".." in self.database_path.parts
        ):
            raise ValueError("scope paths must be canonical absolute paths")

    @property
    def root(self) -> Path:
        return self.revision._root

    @property
    def catalog_uuid(self) -> str:
        return self.revision._catalog_uuid

    def _data(self) -> dict[str, object]:
        return {"database": str(self.database_path), "revision": revision_data(self.revision)}


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """Immutable direction and baseline of one attempt; grants no authority."""

    operation_id: str
    kind: OperationKind
    target: OperationScope
    source: OperationScope | None = None
    keep_last: int | None = 2
    predecessor_id: str | None = None

    def __post_init__(self) -> None:
        UUID(self.operation_id)
        if self.predecessor_id is not None:
            UUID(self.predecessor_id)
            if self.predecessor_id == self.operation_id:
                raise ValueError("an operation cannot supersede itself")
        if self.keep_last is not None:
            positive(self.keep_last)
        if self.kind not in ("sync", "dedup") or (self.kind == "sync") != (self.source is not None):
            raise ValueError("sync requires a source; dedup requires only a local target")
        if self.source is not None:
            left, right = self.source.root, self.target.root
            if left == right or left in right.parents or right in left.parents:
                raise ValueError("sync roots must be disjoint")

    @classmethod
    def sync(cls, source: OperationScope, target: OperationScope, *, keep_last: int | None = 2) -> OperationSpec:
        return cls(str(uuid4()), "sync", target, source, keep_last)

    def _data(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind,
            "target": self.target._data(),
            "source": None if self.source is None else self.source._data(),
            "keep_last": self.keep_last,
            "predecessor_id": self.predecessor_id,
        }

    def _json(self) -> str:
        return canonical(self._data())

    @classmethod
    def _decode(cls, raw: str) -> OperationSpec:
        value = json.loads(raw)

        def scope(data: object) -> OperationScope:
            if not isinstance(data, dict) or set(data) != {"database", "revision"}:
                raise OperationError("invalid endpoint")
            if not isinstance(data["database"], str):
                raise OperationError("invalid database path")
            return OperationScope(Path(data["database"]), decode_revision(data["revision"]))

        if not isinstance(value, dict) or set(value) != {
            "operation_id",
            "kind",
            "target",
            "source",
            "keep_last",
            "predecessor_id",
        }:
            raise OperationError("invalid operation scope")
        result = cls(
            value["operation_id"],
            value["kind"],
            scope(value["target"]),
            None if value["source"] is None else scope(value["source"]),
            value["keep_last"],
            value["predecessor_id"],
        )
        if result._json() != raw:
            raise OperationError("noncanonical operation scope")
        return result


@dataclass(frozen=True, slots=True)
class ManagedRootAuthority:
    """Caller-supplied, nonpersistent authority for one attempt and managed root."""

    operation_id: str
    root: Path
    kind: OperationKind

    def validate(self, spec: OperationSpec) -> None:
        if (self.operation_id, safe_root(self.root), self.kind) != (spec.operation_id, spec.target.root, spec.kind):
            raise OperationError("authority does not match the operation")


@dataclass(frozen=True, slots=True)
class OperationCounts:
    """Disjoint identity outcomes and distinct placed/registered byte totals."""

    planned: int = 0
    planned_bytes: int = 0
    skipped: int = 0
    placed: int = 0
    placed_bytes: int = 0
    conflicts: int = 0
    failed: int = 0
    unattempted: int = 0
    registered: int = 0
    registered_bytes: int = 0

    def __post_init__(self) -> None:
        if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in asdict(self).values()):
            raise ValueError("counts must be nonnegative SQLite integers")
        if self.skipped + self.placed + self.conflicts + self.failed + self.unattempted != self.planned:
            raise ValueError("outcome counts do not partition the planned identities")
        if self.registered > self.placed or self.registered_bytes > self.placed_bytes:
            raise ValueError("registration exceeds verified placements")
        if self.placed_bytes > self.planned_bytes or (self.placed == 0 and self.placed_bytes != 0):
            raise ValueError("placed bytes exceed planned work")

    @property
    def status(self) -> TerminalStatus:
        if not (self.conflicts or self.failed or self.unattempted):
            return "completed"
        return "partial" if self.skipped or self.registered else "failed"


@dataclass(frozen=True, slots=True)
class OperationRecord:
    spec: OperationSpec
    status: str
    started_at_ns: int
    completed_at_ns: int | None
    counts: OperationCounts
    request_digest: str | None = None
    committed_revision: InventoryRevision | None = None


@dataclass(frozen=True, slots=True)
class FinalizationRequest:
    spec: OperationSpec
    counts: OperationCounts
    digest: str

    def __post_init__(self) -> None:
        if len(self.digest) != 64 or any(c not in "0123456789abcdef" for c in self.digest):
            raise ValueError("invalid finalization request digest")


def request_digest(spec: OperationSpec, counts: OperationCounts, rows: Iterable[str]) -> str:
    digest = hashlib.sha256()
    digest.update((canonical({"version": 1, "scope": spec._data(), "counts": asdict(counts)}) + "\n").encode())
    for row in rows:
        digest.update((row + "\n").encode())
    return digest.hexdigest()


def observation_row(path: str, content: ContentId, size: int, mtime: int) -> str:
    return canonical(["upsert", relative(path), content.algorithm, content.digest, size, mtime])


def changes_rows(changes: InventoryChanges) -> Iterable[str]:
    for item in sorted(changes._upserts, key=lambda item: item.relative_path.as_posix()):
        yield observation_row(item.relative_path.as_posix(), item.content_id, item.size_bytes, item.mtime_ns)
    for path in sorted(changes._removals):
        yield canonical(["remove", relative(path.as_posix())])


def prepare_operation_request(
    spec: OperationSpec, counts: OperationCounts, changes: InventoryChanges
) -> FinalizationRequest:
    """Prepare a small database-only request; large manifest ingestion is later work."""
    if changes.baseline != spec.target.revision or changes.location.root != spec.target.root:
        raise OperationError("changes do not match target baseline")
    expected = len(changes._upserts) if spec.kind == "sync" else len(changes._removals)
    if counts.registered != expected or counts.registered_bytes != sum(i.size_bytes for i in changes._upserts):
        raise OperationError("registration counters do not match changes")
    if counts.registered != counts.placed or (spec.kind == "sync" and counts.registered_bytes != counts.placed_bytes):
        raise OperationError("physical successes cannot be omitted from a terminal receipt")
    if (spec.kind == "sync" and changes._removals) or (spec.kind == "dedup" and changes._upserts):
        raise OperationError("changes exceed operation authority")
    return FinalizationRequest(spec, counts, request_digest(spec, counts, changes_rows(changes)))


@dataclass(frozen=True, slots=True)
class PlacementIntent:
    """Artifact names committed before a future copy creates a staging file."""

    staging_directory: str
    witness_path: str
    destination_path: str
    protocol: str = "retained_link_v1"

    def __post_init__(self) -> None:
        for path in (self.staging_directory, self.witness_path, self.destination_path):
            relative(path)
        if self.protocol != "retained_link_v1":
            raise ValueError("unsupported placement protocol")
        if PurePosixPath(self.staging_directory) not in PurePosixPath(self.witness_path).parents:
            raise ValueError("witness must be below staging directory")


@dataclass(frozen=True, slots=True)
class PlacementEvidence:
    """Persisted retained_link_v1 witness; never sufficient without fresh checks."""

    staging_directory: str
    witness_path: str
    destination_path: str
    device: int
    file_id: int
    content_id: ContentId
    size_bytes: int
    mtime_ns: int
    protocol: str = "retained_link_v1"

    def __post_init__(self) -> None:
        for path in (self.staging_directory, self.witness_path, self.destination_path):
            relative(path)
        if (
            self.protocol != "retained_link_v1"
            or any(type(v) is not int for v in (self.file_id, self.device, self.size_bytes, self.mtime_ns))
            or self.file_id <= 0
            or self.device < 0
            or not 0 <= self.size_bytes <= 2**63 - 1
            or not -(2**63) <= self.mtime_ns < 2**63
        ):
            raise ValueError("invalid retained-link evidence")
        if not isinstance(self.content_id, ContentId):
            raise TypeError("placement content identity must be ContentId")
        if PurePosixPath(self.staging_directory) not in PurePosixPath(self.witness_path).parents:
            raise ValueError("witness must be below its owned staging directory")


@dataclass(frozen=True, slots=True)
class PlacementCheck:
    """Fresh facts supplied by a future narrow filesystem adapter, not saved flags."""

    witness_regular: bool
    witness_device: int | None
    witness_file_id: int | None
    destination_exists: bool
    same_file: bool
    destination_content: ContentId | None
    destination_size: int | None
    destination_mtime_ns: int | None


def recover_placement(evidence: PlacementEvidence, check: PlacementCheck, *, frozen: bool) -> str:
    """Decide conservatively without performing file access or granting authority."""
    if not check.witness_regular or (check.witness_device, check.witness_file_id) != (
        evidence.device,
        evidence.file_id,
    ):
        return "reconciliation_required"
    if not check.destination_exists:
        return "reconciliation_required" if frozen else "pending"
    if not check.same_file:
        return "reconciliation_required" if frozen else "conflict"
    if (check.destination_content, check.destination_size) != (evidence.content_id, evidence.size_bytes):
        return "reconciliation_required"
    if frozen and check.destination_mtime_ns != evidence.mtime_ns:
        return "reconciliation_required"
    return "placed"
