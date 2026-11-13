"""Bounded, operation-scoped sync storage. No copying, deletion, or executor."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from itertools import islice
from pathlib import Path, PurePosixPath
from time import time_ns
from typing import TYPE_CHECKING
from uuid import uuid4

from .filesystem import targeted_path
from .models import ContentId
from .operations import (
    FinalizationRequest,
    ManagedRootAuthority,
    OperationCounts,
    OperationError,
    OperationRecord,
    OperationSpec,
    PlacementCheck,
    PlacementEvidence,
    PlacementIntent,
    canonical,
    observation_row,
    positive,
    recover_placement,
    relative,
    request_digest,
    safe_root,
)

if TYPE_CHECKING:
    from .catalog import Catalog

_FORMAT = 1
_STATES = frozenset({"pending", "copying", "verified", "placed", "skipped", "conflict", "failed"})
_SCHEMA = """
CREATE TABLE header (singleton INTEGER PRIMARY KEY CHECK(singleton=1), format INTEGER NOT NULL,
    spec TEXT NOT NULL, state TEXT NOT NULL, phase TEXT, created_at_ns INTEGER NOT NULL,
    expected_count INTEGER NOT NULL, expected_bytes INTEGER NOT NULL, frozen TEXT,
    interrupted_build INTEGER NOT NULL DEFAULT 0);
CREATE TABLE entries (
    entry_id INTEGER PRIMARY KEY, algorithm TEXT NOT NULL, digest TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), source_path TEXT NOT NULL,
    source_mtime_ns INTEGER NOT NULL, destination_path TEXT, destination_key TEXT UNIQUE,
    action TEXT NOT NULL CHECK(action IN ('copy','skip_present','register_existing_verified')),
    state TEXT NOT NULL CHECK(state IN ('pending','copying','verified','placed','skipped','conflict','failed')),
    evidence TEXT, error_code TEXT, error_detail TEXT, predecessor_entry INTEGER, intent TEXT,
    UNIQUE(algorithm,digest));
CREATE INDEX entries_identity ON entries(algorithm,digest);
CREATE TABLE frozen_entries (entry_id INTEGER PRIMARY KEY REFERENCES entries(entry_id), row TEXT NOT NULL);
CREATE TABLE handoffs (entry_id INTEGER PRIMARY KEY REFERENCES entries(entry_id),
    successor_id TEXT NOT NULL, acknowledged INTEGER NOT NULL CHECK(acknowledged IN (0,1)));
"""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    content_id: ContentId
    size_bytes: int
    source_relative_path: PurePosixPath
    source_mtime_ns: int
    destination_relative_path: PurePosixPath | None
    action: str = "copy"
    state: str = "pending"
    entry_id: int | None = None
    evidence: PlacementEvidence | None = None
    error_code: str | None = None
    error_detail: str | None = None
    predecessor_entry: int | None = None
    placement_intent: PlacementIntent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content_id, ContentId):
            raise TypeError("manifest content identity must be ContentId")
        relative(self.source_relative_path.as_posix())
        if self.destination_relative_path is not None:
            relative(self.destination_relative_path.as_posix())
        if type(self.size_bytes) is not int or not 0 <= self.size_bytes <= 2**63 - 1:
            raise ValueError("invalid manifest size")
        if type(self.source_mtime_ns) is not int or not -(2**63) <= self.source_mtime_ns < 2**63:
            raise ValueError("invalid source mtime")
        if self.action not in ("copy", "skip_present", "register_existing_verified") or self.state not in _STATES:
            raise ValueError("invalid manifest action/state")
        if self.action == "skip_present" and (self.state != "skipped" or self.destination_relative_path is not None):
            raise ValueError("catalog-based skips have no destination")
        if self.action != "skip_present" and self.state == "skipped":
            raise ValueError("only represented identities may be skipped")


def _evidence(raw: str | None) -> PlacementEvidence | None:
    if raw is None:
        return None
    data = json.loads(raw)
    data["content_id"] = ContentId(**data["content_id"])
    return PlacementEvidence(**data)


class SyncWorkspace:
    """Persistent temporary manifest handle; closing never deletes recovery evidence."""

    def __init__(self, directory: Path, connection: sqlite3.Connection, spec: OperationSpec, token: str | None) -> None:
        self.directory = directory
        self.spec = spec
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._token = token
        self._closed = False

    @classmethod
    def create(
        cls,
        parent: Path,
        spec: OperationSpec,
        *,
        expected_count: int,
        expected_bytes: int,
    ) -> SyncWorkspace:
        """Create an exclusive format-1 workspace outside both content roots."""
        if spec.kind != "sync" or spec.source is None:
            raise ValueError("sync workspaces require a sync scope")
        if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in (expected_count, expected_bytes)):
            raise ValueError("invalid comparison totals")
        parent = safe_root(parent)
        for root in (spec.source.root, spec.target.root):
            if safe_root(root) != root:
                raise ValueError("workspace scope root is not canonical")
            if parent == root or root in parent.parents:
                raise ValueError("workspace must be outside content roots")
        if os.path.samefile(spec.source.root, spec.target.root):
            raise ValueError("sync roots must be physically disjoint")
        directory = parent / f"archiver-{spec.operation_id}"
        directory.mkdir()
        # Retain even an incomplete creation for explicit inspection/disposal.
        (directory / "owner.json").write_text(
            canonical({"operation_id": spec.operation_id, "format": _FORMAT}), encoding="utf-8"
        )
        connection = sqlite3.connect(directory / "workspace.sqlite3")
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.executescript(_SCHEMA)
            with connection:
                connection.execute(
                    "INSERT INTO header VALUES(1,?,?, 'building',NULL,?,?,?,NULL,0)",
                    (_FORMAT, spec._json(), time_ns(), expected_count, expected_bytes),
                )
            token = cls._lease(directory, takeover=False)
            return cls(directory, connection, spec, token)
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _lease(directory: Path, *, takeover: bool) -> str:
        path = directory / "controller.json"
        if takeover:
            if path.is_symlink() or path.is_junction():
                raise OperationError("unsafe lease artifact")
            path.unlink(missing_ok=True)
        token = str(uuid4())
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(token)
        except FileExistsError as error:
            raise OperationError(
                "workspace has a controller; explicit takeover requires the old controller to be stopped"
            ) from error
        return token

    @classmethod
    def open(cls, directory: Path, *, writable: bool = False, takeover: bool = False) -> SyncWorkspace:
        """Inspect without writes by default; explicit takeover never infers PID liveness."""
        directory = safe_root(directory)
        path = directory / "workspace.sqlite3"
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise OperationError("workspace database is missing or unsafe")
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        token = None
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM header WHERE singleton=1").fetchone()
            if row is None or type(row["format"]) is not int or row["format"] != _FORMAT:
                raise OperationError("unsupported workspace format")
            spec = OperationSpec._decode(row["spec"])
            marker = directory / "owner.json"
            if (
                marker.is_symlink()
                or marker.is_junction()
                or marker.read_text(encoding="utf-8")
                != canonical({"operation_id": spec.operation_id, "format": _FORMAT})
                or directory.name != f"archiver-{spec.operation_id}"
            ):
                raise OperationError("workspace ownership marker mismatch")
            if spec.source is None or any(
                directory == root or root in directory.parents for root in (spec.source.root, spec.target.root)
            ):
                raise OperationError("unsafe workspace scope")
            # Validate complete records in bounded traversal, including malformed unused entries.
            probe = cls(directory, connection, spec, None)
            probe._validate_database()
            if takeover and not writable:
                raise ValueError("takeover requires writable mode")
            if writable:
                token = cls._lease(directory, takeover=takeover)
                connection.close()
                connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
            else:
                connection.execute("PRAGMA query_only=ON")
            result = cls(directory, connection, spec, token)
            if writable:
                with connection:
                    if result.state in ("executing", "finalizing"):
                        connection.execute("UPDATE header SET phase=state,state='recovery_required'")
                    elif result.state == "building":
                        connection.execute("UPDATE header SET interrupted_build=1")
            return result
        except BaseException:
            connection.close()
            if token is not None:
                lease = directory / "controller.json"
                if lease.read_text(encoding="utf-8") == token:
                    lease.unlink()
            raise

    def _validate_database(self) -> None:
        def signature(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
            return tuple(
                (str(row[0]), str(row[1]), " ".join(str(row[2]).split()))
                for row in connection.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
                )
            )

        with closing(sqlite3.connect(":memory:")) as reference:
            reference.executescript(_SCHEMA)
            if signature(reference) != signature(self._connection):
                raise OperationError("workspace tables/constraints do not match format 1")
        if self.state not in {
            "building",
            "ready",
            "executing",
            "finalizing",
            "recovery_required",
            "completed",
            "partial",
            "failed",
            "discarded",
        }:
            raise OperationError("invalid workspace state")
        if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise OperationError("broken workspace foreign key")
        for table, columns in (
            ("header", "phase,created_at_ns,expected_count,expected_bytes,frozen,interrupted_build"),
            ("frozen_entries", "entry_id,row"),
            ("handoffs", "entry_id,successor_id,acknowledged"),
        ):
            self._connection.execute(f"SELECT {columns} FROM {table} LIMIT 0").close()
        for batch in self.batches():
            for entry in batch:
                self._validate_entry(entry)
        self.summary()
        frozen = self.frozen_request
        if frozen is not None:
            rows = (
                str(row[0])
                for row in self._connection.execute(
                    "SELECT row FROM frozen_entries ORDER BY json_extract(row,'$[1]') COLLATE BINARY"
                )
            )
            if request_digest(self.spec, frozen.counts, rows) != frozen.digest:
                raise OperationError("frozen request corruption")
        header = self._connection.execute("SELECT * FROM header").fetchone()
        if header["expected_count"] < 0 or header["expected_bytes"] < 0 or header["interrupted_build"] not in (0, 1):
            raise OperationError("invalid workspace header totals/flags")
        if self.state == "finalizing" and frozen is None:
            raise OperationError("finalizing workspace has no frozen request")

    def _write(self) -> None:
        if self._closed or self._token is None:
            raise OperationError("workspace has no writable controller")
        path = self.directory / "controller.json"
        if path.is_symlink() or path.read_text(encoding="utf-8") != self._token:
            raise OperationError("workspace controller lost ownership")

    @property
    def state(self) -> str:
        row = self._connection.execute("SELECT state FROM header").fetchone()
        if row is None:
            raise OperationError("missing workspace header")
        return str(row[0])

    def _set_state(self, state: str) -> None:
        self._connection.execute("UPDATE header SET state=?", (state,))

    def _validate_entry(self, entry: ManifestEntry) -> None:
        assert self.spec.source is not None
        for scope, path in (
            (self.spec.source, entry.source_relative_path),
            (self.spec.target, entry.destination_relative_path),
        ):
            if path is None:
                continue
            # Reopening/inspection must not require online content roots. Actual
            # ancestor checks run on insertion and again before execution.
            absolute = scope.root / Path(path.as_posix())
            controls = tuple(
                Path(str(database) + suffix)
                for database in (self.spec.source.database_path, self.spec.target.database_path)
                for suffix in ("", "-wal", "-shm", "-journal")
            )
            if absolute in controls or any(part.startswith(".archiver-") for part in path.parts):
                raise OperationError("control artifacts cannot be manifest content")
        if entry.evidence is not None:
            e = entry.evidence
            if (e.content_id, e.size_bytes, e.destination_path) != (
                entry.content_id,
                entry.size_bytes,
                None if entry.destination_relative_path is None else entry.destination_relative_path.as_posix(),
            ):
                raise OperationError("evidence does not match entry")
            owner_id = (
                self.spec.predecessor_id if entry.action == "register_existing_verified" else self.spec.operation_id
            )
            if e.staging_directory != f".archiver-{owner_id}":
                raise OperationError("staging directory does not belong to this attempt")
            expected_intent = PlacementIntent(e.staging_directory, e.witness_path, e.destination_path, e.protocol)
            if entry.placement_intent is not None and entry.placement_intent != expected_intent:
                raise OperationError("evidence differs from committed placement intent")
        if entry.placement_intent is not None:
            intent = entry.placement_intent
            owner_id = (
                self.spec.predecessor_id if entry.action == "register_existing_verified" else self.spec.operation_id
            )
            if (
                intent.staging_directory != f".archiver-{owner_id}"
                or entry.destination_relative_path is None
                or intent.destination_path != entry.destination_relative_path.as_posix()
            ):
                raise OperationError("placement intent exceeds operation scope")
        if entry.action == "register_existing_verified" and (
            self.spec.predecessor_id is None or entry.predecessor_entry is None or entry.evidence is None
        ):
            raise OperationError("recovery entries require predecessor and placement evidence")

    def add(self, entries: Iterable[ManifestEntry], *, batch_size: int = 1000) -> None:
        """Insert one bounded batch transactionally; never consume unbounded input."""
        self._write()
        positive(batch_size)
        batch = tuple(islice(iter(entries), min(batch_size, 10000) + 1))
        if len(batch) > min(batch_size, 10000):
            raise ValueError("insertion exceeds batch maximum (individual batches capped at 10000)")
        if self.state != "building" or self._connection.execute("SELECT interrupted_build FROM header").fetchone()[0]:
            raise OperationError("workspace is not an uninterrupted build")
        with self._connection:
            for entry in batch:
                if entry.entry_id is not None or entry.state not in ("pending", "skipped"):
                    raise OperationError("new entries must be pending or catalog-based skips")
                self._validate_entry(entry)
                assert self.spec.source is not None
                targeted_path(self.spec.source.root, Path(entry.source_relative_path.as_posix()))
                if entry.destination_relative_path is not None:
                    targeted_path(self.spec.target.root, Path(entry.destination_relative_path.as_posix()))
                destination = (
                    None if entry.destination_relative_path is None else entry.destination_relative_path.as_posix()
                )
                key = None if destination is None else os.path.normcase(destination).replace("\\", "/")
                if key is not None:
                    for parent in PurePosixPath(key).parents:
                        if (
                            parent.as_posix() != "."
                            and self._connection.execute(
                                "SELECT 1 FROM entries WHERE destination_key=?", (parent.as_posix(),)
                            ).fetchone()
                        ):
                            raise OperationError("destination has a file ancestor")
                    if self._connection.execute(
                        "SELECT 1 FROM entries WHERE substr(destination_key,1,?)=? LIMIT 1", (len(key) + 1, key + "/")
                    ).fetchone():
                        raise OperationError("destination is an ancestor of another file")
                self._connection.execute(
                    """INSERT INTO entries(algorithm,digest,size_bytes,source_path,source_mtime_ns,
                       destination_path,destination_key,action,state,evidence,predecessor_entry,intent)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        entry.content_id.algorithm,
                        entry.content_id.digest,
                        entry.size_bytes,
                        entry.source_relative_path.as_posix(),
                        entry.source_mtime_ns,
                        destination,
                        key,
                        entry.action,
                        entry.state,
                        None if entry.evidence is None else canonical(asdict(entry.evidence)),
                        entry.predecessor_entry,
                        None if entry.placement_intent is None else canonical(asdict(entry.placement_intent)),
                    ),
                )

    @staticmethod
    def _entry(row: sqlite3.Row) -> ManifestEntry:
        expected_key = (
            None if row["destination_path"] is None else os.path.normcase(row["destination_path"]).replace("\\", "/")
        )
        if row["destination_key"] != expected_key:
            raise OperationError("destination reservation differs from its path")
        if row["state"] in ("verified", "placed") and (row["evidence"] is None or row["intent"] is None):
            raise OperationError("verified placement lacks evidence or committed intent")
        return ManifestEntry(
            ContentId(row["algorithm"], row["digest"]),
            row["size_bytes"],
            PurePosixPath(relative(row["source_path"])),
            row["source_mtime_ns"],
            None if row["destination_path"] is None else PurePosixPath(relative(row["destination_path"])),
            row["action"],
            row["state"],
            row["entry_id"],
            _evidence(row["evidence"]),
            row["error_code"],
            row["error_detail"],
            row["predecessor_entry"],
            None if row["intent"] is None else PlacementIntent(**json.loads(row["intent"])),
        )

    def batches(self, *, batch_size: int = 1000) -> Iterator[tuple[ManifestEntry, ...]]:
        """Return an explicitly closeable bounded iterator in full identity order."""
        positive(batch_size)

        def iterate() -> Iterator[tuple[ManifestEntry, ...]]:
            with closing(self._connection.execute("SELECT * FROM entries ORDER BY algorithm,digest")) as cursor:
                while rows := cursor.fetchmany(min(batch_size, 10000)):
                    yield tuple(self._entry(row) for row in rows)

        return iterate()

    def summary(self) -> OperationCounts:
        if self.state in ("completed", "partial", "failed"):
            request = self.frozen_request
            if request is not None:
                return request.counts
        row = self._connection.execute(
            """SELECT count(*) AS planned,coalesce(sum(size_bytes),0) AS planned_bytes,
            coalesce(sum(state='skipped'),0) AS skipped,coalesce(sum(state='placed'),0) AS placed,
            coalesce(sum(CASE WHEN state='placed' THEN size_bytes ELSE 0 END),0) AS placed_bytes,
            coalesce(sum(state='conflict'),0) AS conflicts,coalesce(sum(state='failed'),0) AS failed,
            coalesce(sum(state IN ('pending','copying','verified')),0) AS unattempted FROM entries"""
        ).fetchone()
        return OperationCounts(**dict(row))

    def mark_ready(self, *, comparison_exhausted: bool) -> None:
        self._write()
        if comparison_exhausted is not True or self.state != "building":
            raise OperationError("readiness requires an exhausted uninterrupted build")
        row = self._connection.execute("SELECT * FROM header").fetchone()
        counts = self.summary()
        if (
            row["interrupted_build"]
            or counts.planned != row["expected_count"]
            or counts.planned_bytes != row["expected_bytes"]
        ):
            raise OperationError("comparison totals do not match build")
        if self._connection.execute(
            "SELECT 1 FROM entries WHERE action!='skip_present' AND destination_path IS NULL"
        ).fetchone():
            raise OperationError("unresolved destinations")
        with self._connection:
            self._set_state("ready")

    def _validate_scopes(self, source: Catalog, target: Catalog) -> None:
        for expected, catalog in ((self.spec.source, source), (self.spec.target, target)):
            assert expected is not None
            current = catalog.operation_scope(expected.root)
            if current != expected:
                raise OperationError("operation scope/baseline changed; explicit replanning required")
        assert self.spec.source is not None
        if os.path.samefile(self.spec.source.root, self.spec.target.root):
            raise OperationError("operation roots are aliases")
        for batch in self.batches():
            for entry in batch:
                targeted_path(self.spec.source.root, Path(entry.source_relative_path.as_posix()))
                if entry.destination_relative_path is not None:
                    targeted_path(self.spec.target.root, Path(entry.destination_relative_path.as_posix()))

    def begin(self, source: Catalog, target: Catalog, *, authority: ManagedRootAuthority) -> None:
        """Record an attempt start only; does not execute any manifest action."""
        self._write()
        authority.validate(self.spec)
        if self.state != "ready":
            raise OperationError("only a ready workspace can start")
        self._validate_scopes(source, target)
        target._start_operation(self.spec)
        with self._connection:
            self._set_state("executing")

    def record_outcome(
        self,
        entry_id: int,
        state: str,
        *,
        evidence: PlacementEvidence | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Persist a supplied executor outcome; saved placement requires later fresh checks."""
        self._write()
        if self.state != "executing" or self.frozen_request is not None:
            raise OperationError("outcomes cannot change outside unfrozen execution")
        row = self._connection.execute("SELECT * FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
        if row is None:
            raise OperationError("unknown entry")
        old = self._entry(row)
        allowed = {
            "pending": {"copying", "conflict", "failed"},
            "copying": {"verified", "conflict", "failed"},
            "verified": {"placed", "conflict", "failed"},
            "placed": set(),
            "skipped": set(),
            "conflict": set(),
            "failed": set(),
        }
        if state not in allowed[old.state]:
            raise OperationError("illegal entry transition")
        evidence = old.evidence if evidence is None else evidence
        intent = old.placement_intent
        if state == "copying" and intent is None:
            if old.destination_relative_path is None:
                raise OperationError("copy intent requires a destination")
            staging = f".archiver-{self.spec.operation_id}"
            intent = PlacementIntent(
                staging, f"{staging}/{old.content_id.digest}.copy", old.destination_relative_path.as_posix()
            )
        updated = replace(
            old,
            state=state,
            evidence=evidence,
            placement_intent=intent,
            error_code=error_code,
            error_detail=error_detail,
        )
        self._validate_entry(updated)
        if state in ("verified", "placed") and evidence is None:
            raise OperationError("verified/placed outcomes need retained-link evidence")
        with self._connection:
            self._connection.execute(
                "UPDATE entries SET state=?,evidence=?,error_code=?,error_detail=?,intent=? WHERE entry_id=?",
                (
                    state,
                    None if evidence is None else canonical(asdict(evidence)),
                    error_code,
                    error_detail,
                    None if intent is None else canonical(asdict(intent)),
                    entry_id,
                ),
            )

    @property
    def frozen_request(self) -> FinalizationRequest | None:
        row = self._connection.execute("SELECT frozen FROM header").fetchone()
        if row[0] is None:
            return None
        data = json.loads(row[0])
        if set(data) != {"version", "counts", "digest"} or type(data["version"]) is not int or data["version"] != 1:
            raise OperationError("invalid frozen request encoding")
        return FinalizationRequest(self.spec, OperationCounts(**data["counts"]), data["digest"])

    def freeze(self) -> FinalizationRequest:
        """Freeze exact selected observations and counts; does not publish them."""
        self._write()
        if self.state != "executing" or self.frozen_request is not None:
            raise OperationError("only unfrozen execution can finalize")
        if self._connection.execute("SELECT 1 FROM entries WHERE state IN ('copying','verified')").fetchone():
            raise OperationError("uncertain in-flight outcomes require recovery")
        counts = self.summary()
        counts = replace(counts, registered=counts.placed, registered_bytes=counts.placed_bytes)
        with self._connection:
            for batch in self.batches():
                for entry in batch:
                    if entry.state == "placed":
                        assert entry.evidence is not None and entry.destination_relative_path is not None
                        self._connection.execute(
                            "INSERT INTO frozen_entries VALUES(?,?)",
                            (
                                entry.entry_id,
                                observation_row(
                                    entry.destination_relative_path.as_posix(),
                                    entry.content_id,
                                    entry.size_bytes,
                                    entry.evidence.mtime_ns,
                                ),
                            ),
                        )
            rows = (
                str(row[0])
                for row in self._connection.execute(
                    "SELECT row FROM frozen_entries ORDER BY json_extract(row,'$[1]') COLLATE BINARY"
                )
            )
            request = FinalizationRequest(self.spec, counts, request_digest(self.spec, counts, rows))
            self._connection.execute(
                "UPDATE header SET frozen=?,state='finalizing'",
                (canonical({"version": 1, "counts": asdict(counts), "digest": request.digest}),),
            )
        return request

    def acknowledge(self, target: Catalog) -> OperationRecord | None:
        """Receipt-first acknowledgement works even after baselines advance/prune."""
        self._write()
        target._validate_operation_target(self.spec)
        record = target.operation(self.spec.operation_id)
        if record is None or record.request_digest is None:
            return None
        request = self.frozen_request
        if request is None or record.spec != self.spec or record.request_digest != request.digest:
            raise OperationError("receipt does not match frozen request")
        with self._connection:
            self._set_state(record.status)
        return record

    def require_recovery(self) -> None:
        self._write()
        if self.state not in ("executing", "finalizing"):
            raise OperationError("only active attempts can require recovery")
        with self._connection:
            self._connection.execute("UPDATE header SET phase=state,state='recovery_required'")

    def recover_entry(self, entry_id: int, check: PlacementCheck) -> str:
        """Apply a supplied fresh evidence check, preserving frozen records verbatim."""
        self._write()
        if self.state != "recovery_required":
            raise OperationError("entry recovery requires explicit recovery mode")
        row = self._connection.execute("SELECT * FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
        if row is None:
            raise OperationError("unknown recovery entry")
        entry = self._entry(row)
        if entry.evidence is None:
            raise OperationError("placement intent/witness evidence is missing")
        decision = recover_placement(entry.evidence, check, frozen=self.frozen_request is not None)
        if self.frozen_request is None and decision != "reconciliation_required":
            with self._connection:
                self._connection.execute("UPDATE entries SET state=? WHERE entry_id=?", (decision, entry_id))
        return decision

    def resume(
        self,
        source: Catalog,
        target: Catalog,
        *,
        authority: ManagedRootAuthority,
        checks: Iterable[tuple[int, PlacementCheck]],
    ) -> None:
        """Explicitly revalidate all uncertain/placed entries in bounded traversal."""
        self._write()
        authority.validate(self.spec)
        if self.acknowledge(target) is not None:
            return
        if self.state != "recovery_required":
            raise OperationError("workspace does not require recovery")
        record = target.operation(self.spec.operation_id)
        if record is None or record.status != "executing" or record.spec != self.spec:
            raise OperationError("operation is missing or fenced against resume")
        self._validate_scopes(source, target)
        with self._connection:
            self._connection.execute("CREATE TEMP TABLE IF NOT EXISTS recovery_checked(entry_id INTEGER PRIMARY KEY)")
            self._connection.execute("DELETE FROM recovery_checked")
        try:
            for entry_id, check in checks:
                decision = self.recover_entry(entry_id, check)
                if decision == "reconciliation_required":
                    raise OperationError("evidence invalidated; explicit replacement plan required")
                with self._connection:
                    self._connection.execute("INSERT INTO recovery_checked VALUES(?)", (entry_id,))
            if self._connection.execute(
                """SELECT 1 FROM entries WHERE (state IN ('copying','verified','placed') OR evidence IS NOT NULL)
                   AND entry_id NOT IN (SELECT entry_id FROM recovery_checked) LIMIT 1"""
            ).fetchone():
                raise OperationError("fresh checks are missing for uncertain/placed entries")
            with self._connection:
                self._set_state("finalizing" if self.frozen_request is not None else "executing")
        finally:
            with self._connection:
                self._connection.execute("DELETE FROM recovery_checked")

    def fail_before_execution(self) -> None:
        self._write()
        if self.state not in ("building", "ready"):
            raise OperationError("physical work may already have begun")
        with self._connection:
            self._set_state("failed")

    def fence(self, target: Catalog, *, superseded: bool = False) -> OperationRecord | None:
        """Fence old-ID publication before explicit discard or replacement planning."""
        self._write()
        target._validate_operation_target(self.spec)
        prior = target.operation(self.spec.operation_id)
        if prior is None:
            if self.state not in ("building", "ready", "failed", "discarded"):
                raise OperationError("started operation is missing")
        else:
            result = target._fence_operation(self.spec, status="superseded" if superseded else "discarded")
            if result.request_digest is not None:
                self.acknowledge(target)
                return result
        with self._connection:
            self._set_state("recovery_required" if superseded else "discarded")
            if superseded:
                self._connection.execute("UPDATE header SET phase='superseded'")
        return None

    def handoff(self, successor: SyncWorkspace, entry_ids: Iterable[int], *, batch_size: int = 1000) -> None:
        """Durably copy recovery evidence only; caller constructs new candidates later."""
        self._write()
        successor._write()
        if (
            successor.spec.predecessor_id != self.spec.operation_id
            or successor.state != "building"
            or self.state != "recovery_required"
            or self._connection.execute("SELECT phase FROM header").fetchone()[0] != "superseded"
            or successor.spec.target.root != self.spec.target.root
            or successor.spec.target.database_path != self.spec.target.database_path
            or successor.spec.target.catalog_uuid != self.spec.target.catalog_uuid
        ):
            raise OperationError("successor must explicitly name this predecessor")
        positive(batch_size)
        ids = tuple(islice(iter(entry_ids), min(batch_size, 10000) + 1))
        if len(ids) > min(batch_size, 10000):
            raise ValueError("handoff exceeds batch size")
        for entry_id in ids:
            row = self._connection.execute("SELECT * FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
            if row is None:
                raise OperationError("unknown predecessor entry")
            entry = self._entry(row)
            if entry.evidence is None or entry.destination_relative_path is None:
                raise OperationError("cannot transfer ambiguous placement evidence")
            # Both durable acknowledgements are retryable; source retains its witness.
            with self._connection:
                self._connection.execute(
                    "INSERT OR IGNORE INTO handoffs VALUES(?,?,0)", (entry_id, successor.spec.operation_id)
                )
                if (
                    self._connection.execute(
                        "SELECT successor_id FROM handoffs WHERE entry_id=?", (entry_id,)
                    ).fetchone()[0]
                    != successor.spec.operation_id
                ):
                    raise OperationError("evidence is already assigned to another successor")
            existing = successor._connection.execute(
                "SELECT * FROM entries WHERE algorithm=? AND digest=?",
                (entry.content_id.algorithm, entry.content_id.digest),
            ).fetchone()
            transferred = replace(
                entry, entry_id=None, state="pending", action="register_existing_verified", predecessor_entry=entry_id
            )
            if existing is None:
                successor.add((transferred,))
            elif replace(successor._entry(existing), entry_id=None) != transferred:
                raise OperationError("successor already has different recovery evidence")
            with self._connection:
                self._connection.execute("UPDATE handoffs SET acknowledged=1 WHERE entry_id=?", (entry_id,))

    def dispose(self, target: Catalog, *, discard: bool = False, artifacts_resolved: bool = False) -> None:
        """Remove only owned workspace files after receipt resolution; never content."""
        self._write()
        if discard:
            self.fence(target)
        elif self.acknowledge(target) is None:
            raise OperationError("no confirmed terminal receipt; retain workspace for recovery/discard")
        if (
            self._connection.execute(
                "SELECT 1 FROM entries WHERE evidence IS NOT NULL OR intent IS NOT NULL LIMIT 1"
            ).fetchone()
            and not artifacts_resolved
        ):
            raise OperationError("owned in-root artifacts remain unresolved; workspace retained")
        if self._connection.execute("SELECT 1 FROM handoffs LIMIT 1").fetchone() and not artifacts_resolved:
            raise OperationError("evidence handoff still requires resolution")
        for batch in self.batches():
            for entry in batch:
                intent = entry.placement_intent
                if intent is not None:
                    witness, _ = targeted_path(self.spec.target.root, Path(intent.witness_path))
                    if witness.exists():
                        raise OperationError("placement witness still exists; executor must resolve it before disposal")
        directory = safe_root(self.directory)
        if directory.name != f"archiver-{self.spec.operation_id}":
            raise OperationError("workspace ownership changed")
        marker = directory / "owner.json"
        if marker.is_symlink() or marker.read_text(encoding="utf-8") != canonical(
            {"operation_id": self.spec.operation_id, "format": _FORMAT}
        ):
            raise OperationError("workspace ownership marker changed")
        allowed = {"owner.json", "workspace.sqlite3", "workspace.sqlite3-journal", "controller.json"}
        for path in directory.iterdir():
            if path.name not in allowed or path.is_symlink() or path.is_junction() or not path.is_file():
                raise OperationError("unknown/unsafe workspace contents; retain for inspection")
        self.close()
        # Enumerate only already-checked exact names; never recurse or remove roots.
        for name in ("workspace.sqlite3-journal", "workspace.sqlite3", "owner.json"):
            path = directory / name
            if path.exists():
                path.unlink()
        directory.rmdir()

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True
        if self._token is not None:
            lease = self.directory / "controller.json"
            if lease.is_file() and not lease.is_symlink() and lease.read_text(encoding="utf-8") == self._token:
                lease.unlink()

    def __enter__(self) -> SyncWorkspace:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
