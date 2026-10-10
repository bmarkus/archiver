from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from archiver import Catalog, FileObservation
from archiver.query import mtime, path, size


def _write(path_value: Path, data: bytes, mtime_ns: int) -> None:
    path_value.parent.mkdir(parents=True, exist_ok=True)
    path_value.write_bytes(data)
    os.utime(path_value, ns=(mtime_ns, mtime_ns))


def _paths(rows: Iterable[FileObservation]) -> list[str]:
    return [row.relative_path.as_posix() for row in rows]


def test_file_query_filters_sorts_limits_and_complete_reductions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root / "a.txt", b"a", 100)
    _write(root / "b.txt", b"bbb", 300)
    _write(root / "nested" / "c.txt", b"cc", 200)
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        base = catalog.files(root).where(path.glob("*.txt")).limit(2)
        assert _paths(base.fetch()) == ["a.txt", "b.txt"]
        assert _paths(base.sort(size.desc()).fetch()) == ["b.txt", "nested/c.txt"]
        assert _paths(base.sort(size.asc()).fetch()) == ["a.txt", "nested/c.txt"]
        assert _paths(base.sort(mtime.desc()).fetch()) == ["b.txt", "nested/c.txt"]
        assert base.count() == 3
        assert base.total_size_bytes() == 6
        assert base.distinct_content_count() == 3


def test_file_query_uses_latest_successful_scan_and_unknown_root_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "root"
    unknown = tmp_path / "unknown"
    root.mkdir()
    unknown.mkdir()
    (root / "stable").write_bytes(b"stable")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        query = catalog.files(root)
        assert query.count() == 1
        assert catalog.files(unknown).fetch() == ()
