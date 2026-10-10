from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, ContentId, ContentRecord, CurrentFileTagView, TagProvenance, TagUsage
from archiver.query import content, size, tag_name, tag_usage, tags


def _seed(catalog: Catalog, root: Path) -> tuple[ContentId, ContentId]:
    (root / "a.bin").write_bytes(b"shared")
    (root / "copy.bin").write_bytes(b"shared")
    (root / "z.bin").write_bytes(b"other")
    catalog.scan_directory(root)
    shared = catalog.content_for_path(root, PurePosixPath("a.bin"))
    other = catalog.content_for_path(root, PurePosixPath("z.bin"))
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "1")
    catalog.add_content_tag(shared, "alpha", user)
    catalog.add_content_tag(shared, "beta", system)
    catalog.add_content_tag(other, "beta", user)
    return shared, other


def test_file_tag_view_is_bounded_typed_and_keeps_complete_reductions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        shared, _ = _seed(catalog, root)
        base = catalog.files(root).where(content.is_(shared)).sort(size.desc()).limit(1)
        view = base.with_tags(limit=1)

        rows = view.fetch()

        assert isinstance(rows[0], CurrentFileTagView)
        assert rows[0].active_tag_count == 2
        assert rows[0].tags == ("alpha",)
        assert base.count() == 2
        assert base.distinct_content_count() == 1
        assert base.total_size_bytes() == 12
        assert base.with_tags(limit=1, all_tags=True).fetch()[0].tags == ("alpha", "beta")

        with pytest.raises(TypeError):
            base.with_tags(all_tags=1)  # type: ignore[arg-type]


def test_content_query_is_catalog_wide_and_detail_paths_are_root_scoped(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        shared, other = _seed(catalog, root)
        query = catalog.contents().where(tags.all_of("beta")).limit(1)

        assert query.count() == 2
        assert isinstance(query.fetch()[0], ContentRecord)
        assert query.fetch()[0].content_id == min((shared, other), key=lambda item: (item.algorithm, item.digest))
        detail = query.with_details(root=root, path_limit=1, tag_limit=1)
        assert detail.current_path_count() == 3
        assert len(detail.fetch()) == 1
        assert len(detail.fetch()[0].current_paths) == 1
        unknown_row = query.with_details(root=unknown).fetch()[0]
        assert unknown_row.current_path_count == 0
        assert unknown_row.current_paths == ()


def test_vocabulary_query_composes_regex_provenance_sort_and_bounds(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        _seed(catalog, root)
        query = (
            catalog.tags()
            .where(tag_name.regex("a|b"))
            .where(tag_name.regex("a$"))
            .sort(tag_usage.assertion_count.desc())
            .limit(1)
        )

        assert query.count() == 2
        assert query.fetch() == (TagUsage("beta", 2, 2, 1, 1),)
        assert [row.tag for row in catalog.tags().provenance("system").sort(tag_name.asc()).fetch()] == ["beta"]


def test_retired_read_api_names_are_absent() -> None:
    for name in (
        "current_files",
        "search_current_files",
        "find_by_content",
        "search_current_files_with_tags",
        "search_tagged_content",
        "search_content_by_tags",
        "search_available_tags",
    ):
        assert not hasattr(Catalog, name)
