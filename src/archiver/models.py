"""Explicit domain types for catalog data."""

import string
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


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
class Location:
    """A cataloged local filesystem root."""

    id: int
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
class ScanSummary:
    """Counts describing a completed scan."""

    files_observed: int
    total_bytes_observed: int
    distinct_content_count: int
    duplicate_content_group_count: int
