"""Streaming content hashing isolated from catalog persistence."""

from hashlib import sha256
from pathlib import Path

from .models import ContentId

DEFAULT_CHUNK_SIZE = 1024 * 1024


def hash_file(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> ContentId:
    """Return the SHA-256 content identity of a regular file, read in chunks."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    hasher = sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            hasher.update(chunk)
    return ContentId(algorithm="sha256", digest=hasher.hexdigest())
