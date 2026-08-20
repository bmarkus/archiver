"""Read-only, deterministic local-filesystem traversal helpers."""

from collections.abc import Collection, Iterator
from pathlib import Path


def regular_files(root: Path, excluded_directories: Collection[Path]) -> Iterator[Path]:
    """Yield regular files recursively, sorting only each directory's entries."""
    excluded = frozenset(directory.resolve(strict=False) for directory in excluded_directories)
    yield from _regular_files(root, excluded)


def _regular_files(directory: Path, excluded_directories: frozenset[Path]) -> Iterator[Path]:
    for entry in sorted(directory.iterdir(), key=lambda candidate: candidate.name):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.resolve(strict=False) not in excluded_directories:
                yield from _regular_files(entry, excluded_directories)
        elif entry.is_file():
            yield entry
