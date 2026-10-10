from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, ContentRecord, TagProvenance
from archiver.query import content, path, tags


def test_content_queries_include_orphaned_and_untagged_content(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    tagged_path = root / "tagged"
    tagged_path.write_bytes(b"tagged")
    (root / "untagged").write_bytes(b"untagged")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        tagged = catalog.content_for_path(root, PurePosixPath("tagged"))
        catalog.add_content_tag(tagged, "keep", TagProvenance("user", "manual", "1"))
        tagged_path.unlink()
        catalog.scan_directory(root)
        all_rows = catalog.contents().fetch()
        tagged_rows = catalog.contents().where(tags.all_of("keep")).fetch()
        assert all(isinstance(row, ContentRecord) for row in all_rows)
        assert tagged_rows[0].content_id == tagged
        assert catalog.contents().where(content.is_(tagged)).count() == 1


def test_content_details_scope_only_paths_and_keep_complete_totals(tmp_path: Path) -> None:
    root = tmp_path / "root"
    unknown = tmp_path / "unknown"
    root.mkdir()
    unknown.mkdir()
    (root / "a").write_bytes(b"same")
    (root / "copy").write_bytes(b"same")
    provenance = TagProvenance("user", "manual", "1")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        content_id = catalog.content_for_path(root, PurePosixPath("a"))
        for tag in ("alpha", "beta"):
            catalog.add_content_tag(content_id, tag, provenance)
        base = catalog.contents().where(tags.any_of("alpha", "missing")).limit(1)
        detail = base.with_details(root=root, path_limit=1, tag_limit=1)
        row = detail.fetch()[0]
        assert row.current_path_count == 2
        assert len(row.current_paths) == 1
        assert row.active_tag_count == 2
        assert row.tags == ("alpha",)
        assert detail.current_path_count() == 2
        assert base.with_details(root=unknown).fetch()[0].current_path_count == 0
        with pytest.raises(TypeError):
            base.where(path.glob("*"))  # type: ignore[arg-type]
