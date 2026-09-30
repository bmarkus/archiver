from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Generator, Iterable
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from archiver import Catalog, DuplicateGroupHandle
from archiver.query import SortExpression, duplicate, size


def _write_copies(root: Path, prefix: str, content: bytes, count: int) -> None:
    for index in range(count):
        path = root / prefix / f"copy-{index:02}.bin"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)


def _digests(handles: Iterable[DuplicateGroupHandle]) -> list[str]:
    return [handle.content_id.digest for handle in handles]


@pytest.mark.parametrize(
    ("ordering", "metric", "reverse"),
    [
        (size.asc(), lambda handle: handle.size_bytes, False),
        (size.desc(), lambda handle: handle.size_bytes, True),
        (duplicate.file_count.asc(), lambda handle: handle.file_instance_count, False),
        (duplicate.file_count.desc(), lambda handle: handle.file_instance_count, True),
        (
            duplicate.redundant_bytes.asc(),
            lambda handle: handle.potential_redundant_bytes,
            False,
        ),
        (
            duplicate.redundant_bytes.desc(),
            lambda handle: handle.potential_redundant_bytes,
            True,
        ),
    ],
)
def test_duplicate_group_sorting_in_both_directions(
    tmp_path: Path,
    ordering: SortExpression,
    metric: Callable[[DuplicateGroupHandle], int],
    reverse: bool,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _write_copies(root, "empty", b"", 2)
    _write_copies(root, "one", b"a", 2)
    _write_copies(root, "two-a", b"bb", 3)
    _write_copies(root, "two-b", b"cc", 3)
    _write_copies(root, "three", b"ddd", 2)

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        handles = tuple(catalog.duplicates(root).sort(ordering).stream_all())

    expected = sorted(
        handles,
        key=lambda handle: (
            metric(handle),
            handle.content_id.algorithm,
            handle.content_id.digest,
        ),
        reverse=reverse,
    )
    if reverse:
        # Identity ties are always ascending, even for descending primary order.
        expected = sorted(
            handles,
            key=lambda handle: (-metric(handle), handle.content_id.algorithm, handle.content_id.digest),
        )
    assert _digests(handles) == _digests(expected)


def test_duplicate_stream_bounds_are_independent_and_all_terminals_remove_one_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    for index in range(21):
        _write_copies(root, f"group-{index:02}", f"value-{index:02}".encode(), 2)
    _write_copies(root, "many", b"many-members", 25)

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        query = catalog.duplicates(root)

        assert len(tuple(query.stream())) == 20
        all_groups = tuple(query.stream_all())
        assert len(all_groups) == 22
        many = next(handle for handle in all_groups if handle.file_instance_count == 25)
        assert len(tuple(many.stream_members())) == 20
        all_members = tuple(many.stream_all_members())
        assert len(all_members) == 25
        assert len({member.relative_path for member in all_members}) == 25
        assert list(member.relative_path.as_posix() for member in all_members) == sorted(
            member.relative_path.as_posix() for member in all_members
        )

        mixed = catalog.duplicates(root).limit(1).member_limit(3)
        selected = tuple(mixed.stream())
        assert len(selected) == 1
        assert len(tuple(selected[0].stream_members())) == 3
        assert len(tuple(selected[0].stream_all_members())) == selected[0].file_instance_count
        assert len(tuple(mixed.stream_all())) == 22


def test_streams_are_lazy_pin_the_scan_and_do_not_eagerly_load_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    first = root / "first.bin"
    second = root / "nested" / "second.bin"
    second.parent.mkdir()
    first.write_bytes(b"old")
    second.write_bytes(b"old")
    member_compilations = 0

    from archiver import _sqlite_query

    original = _sqlite_query.compile_duplicate_group_members

    def capture(*args: object, **kwargs: object) -> object:
        nonlocal member_compilations
        member_compilations += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_sqlite_query, "compile_duplicate_group_members", capture)

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        stream = cast(Generator[DuplicateGroupHandle, None, None], catalog.duplicates(root).member_limit(1).stream())
        assert member_compilations == 0
        with closing(stream):
            handle = next(stream)
            assert isinstance(handle, DuplicateGroupHandle)
            assert member_compilations == 0
        first.write_bytes(b"new")
        second.write_bytes(b"new")
        catalog.scan_directory(root)

        assert [member.content_id for member in handle.stream_all_members()] == [
            handle.content_id,
            handle.content_id,
        ]
        assert member_compilations == 1
        assert handle.content_id.digest == hashlib.sha256(b"old").hexdigest()


def test_group_and_member_streams_interleave_and_require_an_open_catalog(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _write_copies(root, "alpha", b"alpha", 3)
    _write_copies(root, "bravo", b"bravo", 2)
    catalog = Catalog.create(tmp_path / "catalog.sqlite")
    catalog.scan_directory(root)
    groups = cast(Generator[DuplicateGroupHandle, None, None], catalog.duplicates(root).stream_all())
    first_group = next(groups)
    members = first_group.stream_all_members()
    first_member = next(members)
    second_group = next(groups)
    assert first_member.content_id == first_group.content_id
    assert second_group.content_id != first_group.content_id
    assert tuple(members)
    groups.close()

    late_members = second_group.stream_all_members()
    catalog.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        next(late_members)


def test_empty_unknown_and_unscanned_roots_have_empty_streams(tmp_path: Path) -> None:
    root = tmp_path / "source"
    unknown = tmp_path / "unknown"
    root.mkdir()
    unknown.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        assert tuple(catalog.duplicates(root).stream()) == ()
        assert tuple(catalog.duplicates(root).stream_all()) == ()
        catalog.scan_directory(root)
        assert tuple(catalog.duplicates(root).stream()) == ()
        assert tuple(catalog.duplicates(unknown).stream_all()) == ()
