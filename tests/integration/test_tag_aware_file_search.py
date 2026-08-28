from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from archiver import (
    Catalog,
    CurrentFileSearch,
    ScanFailure,
    TagAwareCurrentFileSearch,
    TagProvenance,
)


def _write_file(path: Path, contents: bytes, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _paths(result: TagAwareCurrentFileSearch) -> list[str]:
    return [item.observation.relative_path.as_posix() for item in result.files]


def test_tag_aware_file_search_composes_tags_provenance_paths_and_bounded_previews(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write_file(root / "a-copy.bin", b"shared", 1_700_000_002_000_000_000)
    _write_file(root / "nested" / "a.bin", b"shared", 1_700_000_001_000_000_000)
    _write_file(root / "b.bin", b"second", 1_700_000_003_000_000_000)
    _write_file(root / "untagged.bin", b"plain", 1_700_000_000_000_000_000)
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "2")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        shared = catalog.content_for_path(root, PurePosixPath("a-copy.bin"))
        second = catalog.content_for_path(root, PurePosixPath("b.bin"))
        for tag in ("alpha", "family"):
            catalog.add_content_tag(shared, tag, user)
        for tag in ("family", "trip:us"):
            catalog.add_content_tag(shared, tag, system)
            catalog.add_content_tag(second, tag, user)
        catalog.add_content_tag(second, "hidden", user)
        catalog.retract_content_tag(second, "hidden", user)

        all_result = catalog.search_current_files_with_tags(
            root,
            tags=("family", "family", "trip:us"),
            limit=10,
            tag_limit=1,
        )
        bounded_result = catalog.search_current_files_with_tags(
            root,
            tags=("family", "trip:us"),
            limit=2,
            tag_limit=1,
        )
        user_result = catalog.search_current_files_with_tags(
            root,
            tags=("family", "trip:us"),
            provenance="user",
        )
        system_nested = catalog.search_current_files_with_tags(
            root,
            path_glob="nested/*",
            tags=("family", "trip:us"),
            provenance="system",
        )
        any_result = catalog.search_current_files_with_tags(
            root,
            tags=("alpha", "trip:us"),
            match="any",
            tag_limit=None,
        )
        display_only = catalog.search_current_files_with_tags(root, tags=(), tag_limit=None)
        retracted_result = catalog.search_current_files_with_tags(root, tags=("hidden",))

    assert isinstance(all_result, TagAwareCurrentFileSearch)
    assert _paths(all_result) == ["a-copy.bin", "b.bin", "nested/a.bin"]
    assert all_result.total_file_count == 3
    assert all_result.total_content_count == 2
    assert all_result.total_file_size_bytes == 18
    assert all_result.files[0].active_tag_count == 3
    assert all_result.files[0].tags == ("alpha",)
    assert all_result.files[0].tags == all_result.files[2].tags
    assert all_result.files[1].active_tag_count == 2
    assert all_result.files[1].tags == ("family",)
    assert len(bounded_result.files) == 2
    assert bounded_result.total_file_count == 3
    assert bounded_result.total_content_count == 2
    assert bounded_result.total_file_size_bytes == 18
    assert _paths(user_result) == ["b.bin"]
    assert _paths(system_nested) == ["nested/a.bin"]
    assert _paths(any_result) == ["a-copy.bin", "b.bin", "nested/a.bin"]
    assert any_result.files[0].tags == ("alpha", "family", "trip:us")
    assert _paths(display_only) == ["a-copy.bin", "b.bin", "nested/a.bin", "untagged.bin"]
    assert display_only.files[-1].active_tag_count == 0
    assert display_only.files[-1].tags == ()
    assert retracted_result.total_file_count == 0
    assert retracted_result.total_content_count == 0
    assert retracted_result.total_file_size_bytes == 0
    assert retracted_result.files == ()


@pytest.mark.parametrize(
    ("sort_by", "reverse"),
    (("path", False), ("path", True), ("size", False), ("size", True), ("date", False), ("date", True)),
)
def test_tag_aware_zero_filter_preserves_current_file_sorting_and_legacy_api(
    tmp_path: Path,
    sort_by: str,
    reverse: bool,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write_file(root / "a.bin", b"a", 1_700_000_000_000_000_000)
    _write_file(root / "b.bin", b"bb", 1_700_000_001_000_000_000)
    _write_file(root / "c.bin", b"cc", 1_700_000_001_000_000_000)

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        legacy = catalog.search_current_files(root, sort_by=sort_by, reverse=reverse)  # type: ignore[arg-type]
        tagged = catalog.search_current_files_with_tags(root, sort_by=sort_by, reverse=reverse)  # type: ignore[arg-type]

    assert isinstance(legacy, CurrentFileSearch)
    assert [item.relative_path for item in legacy.files] == [item.observation.relative_path for item in tagged.files]
    assert legacy.total_matches == tagged.total_file_count
    assert legacy.total_size_bytes == tagged.total_file_size_bytes
    assert tagged.total_content_count == 3


def test_tag_aware_file_search_uses_only_latest_successful_state_after_failures_and_interruptions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.bin"
    source.write_bytes(b"first")
    provenance = TagProvenance("user", "manual", "1")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        first = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        catalog.add_content_tag(first, "first", provenance)

        source.write_bytes(b"second")
        catalog.scan_directory(root)
        second = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        catalog.add_content_tag(second, "second", provenance)
        assert catalog.search_current_files_with_tags(root, tags=("first",)).total_file_count == 0

        source.write_bytes(b"third")

        def fail(_: object) -> None:
            raise RuntimeError("controlled failure")

        with pytest.raises(ScanFailure, match="reconciliation failed"):
            catalog.scan_directory(root, progress_callback=fail)
        after_failure = catalog.search_current_files_with_tags(root, tags=("second",))

        def interrupt(_: object) -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            catalog.scan_directory(root, progress_callback=interrupt)
        after_interrupt = catalog.search_current_files_with_tags(root, tags=("second",))

    assert _paths(after_failure) == ["entry.bin"]
    assert after_failure.files[0].observation.content_id == second
    assert _paths(after_interrupt) == ["entry.bin"]
    assert after_interrupt.files[0].observation.content_id == second


def test_tag_aware_file_search_validates_is_read_only_and_applies_sql_limits(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.bin"
    source.write_bytes(b"entry")
    provenance = TagProvenance("user", "manual", "1")
    database_path = tmp_path / "catalog.sqlite"

    with Catalog.create(database_path) as catalog:
        catalog.scan_directory(root)
        content_id = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        for tag in ("alpha", "beta", "stable"):
            catalog.add_content_tag(content_id, tag, provenance)
        before = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            catalog.tags_for_content(content_id),
            catalog.schema_version,
            source.read_bytes(),
            source.stat().st_mtime_ns,
            source.stat().st_mode,
        )

        statements: list[str] = []
        catalog._connection.set_trace_callback(statements.append)
        try:
            result = catalog.search_current_files_with_tags(
                root,
                tags=("stable",),
                limit=1,
                tag_limit=1,
            )
        finally:
            catalog._connection.set_trace_callback(None)

        with pytest.raises(ValueError, match="tag must match"):
            catalog.search_current_files_with_tags(root, tags=("Invalid Tag",))
        with pytest.raises(ValueError, match="match must"):
            catalog.search_current_files_with_tags(root, match="neither")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="provenance must"):
            catalog.search_current_files_with_tags(root, tags=("stable",), provenance="other")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="provenance requires"):
            catalog.search_current_files_with_tags(root, provenance="user")
        with pytest.raises(ValueError, match="unsupported current-file sort"):
            catalog.search_current_files_with_tags(root, sort_by="other")  # type: ignore[arg-type]
        for arguments in ({"limit": 0}, {"tag_limit": 0}):
            with pytest.raises(ValueError, match="at least 1"):
                catalog.search_current_files_with_tags(root, **arguments)  # type: ignore[arg-type]

        after = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            catalog.tags_for_content(content_id),
            catalog.schema_version,
            source.read_bytes(),
            source.stat().st_mtime_ns,
            source.stat().st_mode,
        )

    traced_sql = "\n".join(statements).upper()
    assert len(result.files) == 1
    assert len(result.files[0].tags) == 1
    assert "LIMIT 1" in traced_sql
    assert "WHERE TAG_RANK <= 1" in traced_sql
    assert after == before
