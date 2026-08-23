"""Explicit domain types for catalog data."""

import re
import string
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypeAlias

CurrentFileSort: TypeAlias = Literal["path", "size", "date"]
RefreshChangeKind: TypeAlias = Literal["new", "unchanged", "modified", "missing"]
TagProvenanceKind: TypeAlias = Literal["user", "system"]

_TAG_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}\Z")


def validate_tag_name(name: str) -> str:
    """Return a valid canonical tag name or raise ``ValueError``."""
    if _TAG_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("tag must match [a-z0-9][a-z0-9._:-]{0,63}")
    return name


@dataclass(frozen=True, slots=True)
class ContentId:
    """A cryptographic identity for a sequence of file bytes."""

    algorithm: str
    digest: str

    def __post_init__(self) -> None:
        if self.algorithm != "sha256":
            raise ValueError("Plan 001 supports only the sha256 content algorithm")
        if len(self.digest) != 64 or any(character not in string.hexdigits for character in self.digest):
            raise ValueError("a SHA-256 digest must contain exactly 64 hexadecimal characters")
        if self.digest != self.digest.lower():
            raise ValueError("content digests must use lowercase hexadecimal")


@dataclass(frozen=True, slots=True)
class TagProvenance:
    """The producer identity attached to one tag assertion."""

    kind: TagProvenanceKind
    source_name: str
    source_version: str
    source_detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("user", "system"):
            raise ValueError("tag provenance kind must be 'user' or 'system'")
        for field_name, value in (("source_name", self.source_name), ("source_version", self.source_version)):
            if not value or value.strip() != value or any(character in "\r\n\0" for character in value):
                raise ValueError(f"tag provenance {field_name} must be a non-empty single-line value")
        if any(character in "\r\n\0" for character in self.source_detail):
            raise ValueError("tag provenance source_detail must be a single-line value")


@dataclass(frozen=True, slots=True)
class ContentTagAssertion:
    """One active provenance-aware tag assertion about content."""

    content_id: ContentId
    tag: str
    provenance: TagProvenance
    asserted_at_ns: int


@dataclass(frozen=True, slots=True)
class TaggedContent:
    """One content identity returned by a reverse tag lookup."""

    content_id: ContentId
    size_bytes: int
    assertions: tuple[ContentTagAssertion, ...]


@dataclass(frozen=True, slots=True)
class TaggedContentSearch:
    """Bounded tagged content together with its complete match count."""

    contents: tuple[TaggedContent, ...]
    total_matches: int


@dataclass(frozen=True, slots=True)
class Location:
    """A cataloged local filesystem root."""

    id: int | None
    root: Path


@dataclass(frozen=True, slots=True)
class FileObservation:
    """A regular file observed in one completed scan."""

    location: Location
    relative_path: PurePosixPath
    content_id: ContentId
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class RefreshChange:
    """One path-level difference between a filesystem observation and the current catalog."""

    kind: RefreshChangeKind
    relative_path: PurePosixPath
    previous: FileObservation | None
    current: FileObservation | None
    hash_reused: bool


@dataclass(frozen=True, slots=True)
class RefreshSummary:
    """Counts of the path changes detected during one reconciliation."""

    new_files: int
    unchanged_files: int
    modified_files: int
    missing_files: int


@dataclass(frozen=True, slots=True)
class RefreshChangeSet:
    """Filesystem observations reconciled against one catalog-current baseline.

    The baseline detects catalog staleness at apply time. It does not lock the
    filesystem or claim that files remain unchanged after reconciliation.
    """

    location: Location
    baseline_scan_id: int | None
    changes: tuple[RefreshChange, ...]

    @property
    def summary(self) -> RefreshSummary:
        """Return deterministic aggregate counts for the change set."""
        counts = {kind: 0 for kind in ("new", "unchanged", "modified", "missing")}
        for change in self.changes:
            counts[change.kind] += 1
        return RefreshSummary(
            new_files=counts["new"],
            unchanged_files=counts["unchanged"],
            modified_files=counts["modified"],
            missing_files=counts["missing"],
        )


@dataclass(frozen=True, slots=True)
class CurrentFileSearch:
    """A bounded current-file query with its total number of matches."""

    files: tuple[FileObservation, ...]
    total_matches: int
    total_size_bytes: int


@dataclass(frozen=True, slots=True)
class ScanRun:
    """One attempted scan of a cataloged location."""

    id: int
    location: Location
    status: str
    started_at_ns: int
    completed_at_ns: int | None


@dataclass(frozen=True, slots=True)
class HistoricalObservation:
    """A file observation together with the scan that recorded it."""

    scan: ScanRun
    relative_path: PurePosixPath
    content_id: ContentId
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Counts describing a completed scan."""

    files_observed: int
    total_bytes_observed: int
    distinct_content_count: int
    duplicate_content_group_count: int


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """Completed work reported while a scan is in progress."""

    files_observed: int
    total_bytes_observed: int
    elapsed_seconds: float
    current_relative_path: PurePosixPath


@dataclass(frozen=True, slots=True)
class DuplicateSummary:
    """Aggregate duplicate metrics for one location's current state."""

    duplicate_content_group_count: int
    duplicate_file_instance_count: int
    potential_redundant_bytes: int


@dataclass(frozen=True, slots=True)
class DuplicateGroupView:
    """One duplicate-content group with a bounded member projection."""

    content_id: ContentId
    size_bytes: int
    file_instance_count: int
    potential_redundant_bytes: int
    members: tuple[FileObservation, ...]


@dataclass(frozen=True, slots=True)
class DuplicateGroupSearch:
    """Bounded duplicate groups together with complete aggregate metrics."""

    groups: tuple[DuplicateGroupView, ...]
    summary: DuplicateSummary
