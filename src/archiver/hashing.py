"""Streaming content hashing isolated from catalog persistence."""

from hashlib import sha256
from os import fstat
from pathlib import Path
from stat import S_ISREG
from typing import BinaryIO

from .models import ContentId

DEFAULT_CHUNK_SIZE = 1024 * 1024


class FileChangedDuringHashingError(OSError):
    """Raised when a regular file changes detectably while its bytes are read."""


def _hash_stream(source: BinaryIO, *, chunk_size: int) -> ContentId:
    hasher = sha256()
    while chunk := source.read(chunk_size):
        hasher.update(chunk)
    return ContentId(algorithm="sha256", digest=hasher.hexdigest())


def _stat_signature(metadata: object) -> tuple[int, int, int, int, int]:
    """Return fields used only to detect a changed file handle or path entry."""
    return (
        int(getattr(metadata, "st_dev")),
        int(getattr(metadata, "st_ino")),
        int(getattr(metadata, "st_mode")),
        int(getattr(metadata, "st_size")),
        int(getattr(metadata, "st_mtime_ns")),
    )


def hash_file(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> ContentId:
    """Return the SHA-256 content identity of a regular file, read in chunks."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    with path.open("rb") as source:
        return _hash_stream(source, chunk_size=chunk_size)


def hash_file_stably(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> tuple[ContentId, int, int]:
    """Hash a stable regular file, rejecting detectable concurrent mutations.

    The file descriptor and pathname must agree before and after bytes are read.
    This cannot prevent a later filesystem change, nor detect a hostile rewrite
    that restores every checked metadata field, but it prevents a detectably
    inconsistent byte/metadata observation from being returned.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    before_path = path.stat()
    if not S_ISREG(before_path.st_mode):
        raise FileChangedDuringHashingError(f"not a regular file: {path}")
    with path.open("rb") as source:
        before_handle = fstat(source.fileno())
        if not S_ISREG(before_handle.st_mode) or _stat_signature(before_path) != _stat_signature(before_handle):
            raise FileChangedDuringHashingError(f"file changed before hashing: {path}")
        content_id = _hash_stream(source, chunk_size=chunk_size)
        after_handle = fstat(source.fileno())
    after_path = path.stat()
    if (
        not S_ISREG(after_handle.st_mode)
        or _stat_signature(before_handle) != _stat_signature(after_handle)
        or _stat_signature(after_handle) != _stat_signature(after_path)
    ):
        raise FileChangedDuringHashingError(f"file changed during hashing: {path}")
    return content_id, int(after_handle.st_size), int(after_handle.st_mtime_ns)
