from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, CurrentFileTagView, TagProvenance
from archiver.query import path, tags


def test_file_tag_views_filter_by_provenance_but_display_all_active_tags(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").write_bytes(b"same")
    (root / "copy").write_bytes(b"same")
    (root / "other").write_bytes(b"other")
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "1")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        shared = catalog.content_for_path(root, PurePosixPath("a"))
        catalog.add_content_tag(shared, "alpha", user)
        catalog.add_content_tag(shared, "beta", system)
        query = catalog.files(root).where(tags.all_of("beta", provenance="system"))
        rows = query.with_tags(limit=1).fetch()
        assert all(isinstance(row, CurrentFileTagView) for row in rows)
        assert [row.observation.relative_path.as_posix() for row in rows] == ["a", "copy"]
        assert rows[0].tags == rows[1].tags == ("alpha",)
        assert rows[0].active_tag_count == 2
        assert query.with_tags(limit=1, all_tags=True).fetch()[0].tags == ("alpha", "beta")
        assert catalog.files(root).where(path.glob("missing*")).with_tags().fetch() == ()


def test_file_tag_view_validates_display_options_eagerly(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        base = catalog.files(root)
        with pytest.raises(ValueError):
            base.with_tags(limit=0)
        with pytest.raises(TypeError):
            base.with_tags(all_tags=1)  # type: ignore[arg-type]
