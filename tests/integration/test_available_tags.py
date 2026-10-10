from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, TagProvenance, TagUsage
from archiver.query import size, tag_name, tag_usage


def _seed(catalog: Catalog, root: Path) -> None:
    catalog.scan_directory(root)
    first = catalog.content_for_path(root, PurePosixPath("first"))
    second = catalog.content_for_path(root, PurePosixPath("second"))
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "1")
    catalog.add_content_tag(first, "alpha", user)
    catalog.add_content_tag(first, "alpha", system)
    catalog.add_content_tag(second, "alpha", user)
    catalog.add_content_tag(first, "beta", user)


def test_vocabulary_counts_sorts_regex_and_provenance(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "first").write_bytes(b"first")
    (root / "second").write_bytes(b"second")
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        _seed(catalog, root)
        rows = catalog.tags().fetch()
        assert rows == (TagUsage("alpha", 2, 3, 2, 1), TagUsage("beta", 1, 1, 1, 0))
        assert catalog.tags().where(tag_name.regex("ph")).count() == 1
        assert [row.tag for row in catalog.tags().sort(tag_usage.assertion_count.desc()).limit(1).fetch()] == ["alpha"]
        assert catalog.tags().provenance("system").fetch() == (TagUsage("alpha", 1, 1, 0, 1),)


def test_vocabulary_validates_eagerly_and_rejects_other_domains(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        query = catalog.tags()
        with pytest.raises(re.error):
            tag_name.regex("[")
        with pytest.raises(TypeError):
            query.where("alpha")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            query.sort(size.asc())
