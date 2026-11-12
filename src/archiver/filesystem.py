"""Read-only, deterministic local-filesystem traversal helpers."""

from collections.abc import Collection, Iterator
from pathlib import Path, PurePosixPath


def regular_files(root: Path, excluded_directories: Collection[Path]) -> Iterator[Path]:
    """Yield files in POSIX component order, sorting only each directory's entries.

    Depth-first traversal places ``dir/file`` before the sibling ``dir.zip``;
    callers merging a second stream must use the same component ordering.
    """
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


def targeted_path(root: Path, supplied: Path) -> tuple[Path, PurePosixPath]:
    """Validate a selected path before resolution; no traversal or file mutation.

    Ordinary checks do not lock ancestors against concurrent replacement.
    Distinct hard-linked names remain distinct file instances.
    """
    if not isinstance(supplied, Path):
        raise TypeError("file paths must be Path values")
    if ".." in supplied.parts:
        raise ValueError("parent traversal is not allowed")
    candidate = supplied if supplied.is_absolute() else root / supplied
    for ancestor in (candidate, *candidate.parents):
        if ancestor.is_symlink() or ancestor.is_junction():
            raise ValueError("selected paths must not traverse symbolic links or junctions")
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("selected path is outside the catalog root") from error
    if not relative.parts:
        raise ValueError("selected path must identify a file")
    return resolved, PurePosixPath(relative.as_posix())
