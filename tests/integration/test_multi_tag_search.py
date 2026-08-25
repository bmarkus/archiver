from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, MultiTagContentSearch, ScanFailure, TaggedContentSearch, TagProvenance


def test_multi_tag_search_matches_all_any_and_provenance_with_bounded_previews(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.bin").write_bytes(b"shared")
    (root / "a-copy.bin").write_bytes(b"shared")
    (root / "b.bin").write_bytes(b"second")
    (root / "c.bin").write_bytes(b"third")
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "2")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        shared = catalog.content_for_path(root, PurePosixPath("a.bin"))
        second = catalog.content_for_path(root, PurePosixPath("b.bin"))
        third = catalog.content_for_path(root, PurePosixPath("c.bin"))

        for tag in ("alpha", "family", "trip:us"):
            catalog.add_content_tag(shared, tag, user)
        catalog.add_content_tag(shared, "family", system)
        catalog.add_content_tag(second, "family", user)
        catalog.add_content_tag(third, "family", system)
        catalog.add_content_tag(third, "trip:us", system)

        all_result = catalog.search_content_by_tags(
            root,
            ("family", "family", "trip:us"),
            path_limit=1,
            tag_limit=2,
        )
        any_result = catalog.search_content_by_tags(root, ("family", "trip:us"), match="any")
        user_result = catalog.search_content_by_tags(root, ("family", "trip:us"), provenance="user", tag_limit=None)
        system_result = catalog.search_content_by_tags(root, ("family", "trip:us"), provenance="system", tag_limit=None)
        legacy_result = catalog.search_tagged_content("family")

    assert isinstance(all_result, MultiTagContentSearch)
    assert all_result.total_matches == 2
    assert all_result.total_current_paths == 3
    assert [item.content_id.digest for item in all_result.contents] == sorted((shared.digest, third.digest))
    by_content = {item.content_id: item for item in all_result.contents}
    assert by_content[shared].current_path_count == 2
    assert len(by_content[shared].current_paths) == 1
    assert by_content[shared].active_tag_count == 3
    assert by_content[shared].tags == ("alpha", "family")
    assert by_content[third].current_path_count == 1
    assert by_content[third].tags == ("family", "trip:us")

    assert any_result.total_matches == 3
    assert any_result.total_current_paths == 4
    assert [item.content_id.digest for item in any_result.contents] == sorted(
        (shared.digest, second.digest, third.digest)
    )
    assert [item.content_id for item in user_result.contents] == [shared]
    assert user_result.contents[0].tags == ("alpha", "family", "trip:us")
    assert [item.content_id for item in system_result.contents] == [third]
    assert isinstance(legacy_result, TaggedContentSearch)
    assert legacy_result.total_matches == 3


def test_multi_tag_search_is_catalog_wide_but_current_paths_are_root_scoped(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_path = first_root / "remember.bin"
    second_path = second_root / "elsewhere.bin"
    first_path.write_bytes(b"remember")
    second_path.write_bytes(b"remember")
    provenance = TagProvenance("user", "manual", "1")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(first_root)
        catalog.scan_directory(second_root)
        content_id = catalog.content_for_path(first_root, PurePosixPath("remember.bin"))
        catalog.add_content_tag(content_id, "remember", provenance)

        first_result = catalog.search_content_by_tags(first_root, ("remember",))
        second_result = catalog.search_content_by_tags(second_root, ("remember",))

        first_path.unlink()
        catalog.scan_directory(first_root)
        orphaned_at_first = catalog.search_content_by_tags(first_root, ("remember",))
        still_current_at_second = catalog.search_content_by_tags(second_root, ("remember",))

    assert first_result.total_matches == 1
    assert first_result.total_current_paths == 1
    assert first_result.contents[0].current_paths == (PurePosixPath("remember.bin"),)
    assert second_result.contents[0].current_paths == (PurePosixPath("elsewhere.bin"),)
    assert orphaned_at_first.total_matches == 1
    assert orphaned_at_first.total_current_paths == 0
    assert orphaned_at_first.contents[0].current_path_count == 0
    assert orphaned_at_first.contents[0].current_paths == ()
    assert still_current_at_second.total_current_paths == 1
    assert still_current_at_second.contents[0].current_paths == (PurePosixPath("elsewhere.bin"),)


def test_multi_tag_search_uses_last_successful_scan_after_failure(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.bin"
    source.write_bytes(b"original")
    provenance = TagProvenance("user", "manual", "1")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        content_id = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        catalog.add_content_tag(content_id, "stable", provenance)
        source.write_bytes(b"replacement")

        def fail_progress(_: object) -> None:
            raise RuntimeError("controlled failure")

        with pytest.raises(ScanFailure, match="reconciliation failed"):
            catalog.scan_directory(root, progress_callback=fail_progress)
        result = catalog.search_content_by_tags(root, ("stable",))

    assert result.total_matches == 1
    assert result.total_current_paths == 1
    assert result.contents[0].content_id == content_id
    assert result.contents[0].current_paths == (PurePosixPath("entry.bin"),)


def test_multi_tag_search_validates_inputs_and_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.bin"
    source.write_bytes(b"entry")
    provenance = TagProvenance("user", "manual", "1")
    database_path = tmp_path / "catalog.sqlite"

    with Catalog.create(database_path) as catalog:
        catalog.scan_directory(root)
        content_id = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        catalog.add_content_tag(content_id, "valid", provenance)
        before = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            catalog.tags_for_content(content_id),
            catalog.schema_version,
            source.read_bytes(),
            source.stat().st_mtime_ns,
            source.stat().st_mode,
        )

        catalog.search_content_by_tags(root, ("valid",))
        with pytest.raises(ValueError, match="at least one tag"):
            catalog.search_content_by_tags(root, ())
        with pytest.raises(ValueError, match="tag must match"):
            catalog.search_content_by_tags(root, ("Invalid Tag",))
        with pytest.raises(ValueError, match="match must"):
            catalog.search_content_by_tags(root, ("valid",), match="neither")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="provenance must"):
            catalog.search_content_by_tags(root, ("valid",), provenance="other")  # type: ignore[arg-type]
        for arguments in ({"limit": 0}, {"path_limit": 0}, {"tag_limit": 0}):
            with pytest.raises(ValueError, match="at least 1"):
                catalog.search_content_by_tags(root, ("valid",), **arguments)  # type: ignore[arg-type]

        after = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            catalog.tags_for_content(content_id),
            catalog.schema_version,
            source.read_bytes(),
            source.stat().st_mtime_ns,
            source.stat().st_mode,
        )

    assert after == before


def test_multi_tag_search_applies_sql_limits_before_preview_rows_are_returned(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(3):
        (root / f"entry-{index}.bin").write_bytes(f"content-{index}".encode())
    provenance = TagProvenance("user", "manual", "1")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        for index in range(3):
            content_id = catalog.content_for_path(root, PurePosixPath(f"entry-{index}.bin"))
            for tag in ("common", "extra-a", "extra-b"):
                catalog.add_content_tag(content_id, tag, provenance)
        statements: list[str] = []
        catalog._connection.set_trace_callback(statements.append)
        try:
            result = catalog.search_content_by_tags(
                root,
                ("common",),
                limit=1,
                path_limit=1,
                tag_limit=1,
            )
        finally:
            catalog._connection.set_trace_callback(None)

    traced_sql = "\n".join(statements).upper()
    assert len(result.contents) == 1
    assert len(result.contents[0].current_paths) == 1
    assert len(result.contents[0].tags) == 1
    assert "LIMIT 1" in traced_sql
    assert "WHERE PATH_RANK <= 1" in traced_sql
    assert "WHERE TAG_RANK <= 1" in traced_sql
