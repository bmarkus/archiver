from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, ContentId, InvalidCatalogError, TaggingError, TagProvenance


def _downgrade_to_v1(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    with connection:
        connection.execute("DROP TABLE content_tag_assertions")
        connection.execute("DROP TABLE tags")
        connection.execute("UPDATE catalog_metadata SET schema_version = 1")
    connection.close()


def test_explicit_v1_migration_preserves_catalog_identity_and_current_state(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.txt"
    source.write_bytes(b"entry")
    database_path = tmp_path / "catalog.sqlite"

    with Catalog.create(database_path) as catalog:
        catalog.scan_directory(root)
        catalog_uuid = catalog.catalog_uuid
        expected = catalog.current_files(root)
    _downgrade_to_v1(database_path)

    with pytest.raises(InvalidCatalogError, match="requires migration"):
        Catalog.open(database_path)

    assert Catalog.migrate(database_path) is True
    assert Catalog.migrate(database_path) is False
    with Catalog.open(database_path) as catalog:
        assert catalog.schema_version == 2
        assert catalog.catalog_uuid == catalog_uuid
        assert catalog.current_files(root) == expected


def test_failed_migration_rolls_back_version_and_partial_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite"
    Catalog.create(database_path).close()
    _downgrade_to_v1(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(InvalidCatalogError, match="could not migrate catalog"):
        Catalog.migrate(database_path)

    connection = sqlite3.connect(database_path)
    version = connection.execute("SELECT schema_version FROM catalog_metadata").fetchone()
    assertion_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'content_tag_assertions'"
    ).fetchone()
    connection.close()
    assert version == (1,)
    assert assertion_table is None


def test_tagging_duplicate_content_shares_tags_and_preserves_source_metadata(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = root / "first.bin"
    second = root / "nested" / "second.bin"
    second.parent.mkdir()
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) for path in (first, second)}
    provenance = TagProvenance("user", "test-suite", "1.0")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        assert catalog.add_tag_for_path(root, PurePosixPath("first.bin"), "favorite", provenance) is True
        assert catalog.add_tag_for_path(root, PurePosixPath("nested/second.bin"), "favorite", provenance) is False
        first_tags = catalog.tags_for_path(root, PurePosixPath("first.bin"))
        second_tags = catalog.tags_for_path(root, PurePosixPath("nested/second.bin"))

    assert first_tags == second_tags
    assert [assertion.tag for assertion in first_tags] == ["favorite"]
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) for path in (first, second)
    } == before


def test_tags_survive_refresh_and_every_path_disappearing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.txt"
    source.write_bytes(b"persistent content")
    provenance = TagProvenance("user", "test-suite", "1.0")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        content_id = catalog.content_for_path(root, PurePosixPath("entry.txt"))
        catalog.add_content_tag(content_id, "remember", provenance)
        catalog.scan_directory(root)
        assert [item.tag for item in catalog.tags_for_content(content_id)] == ["remember"]

        renamed = root / "renamed.txt"
        source.rename(renamed)
        catalog.scan_directory(root)
        assert [item.tag for item in catalog.tags_for_path(root, PurePosixPath("renamed.txt"))] == ["remember"]

        renamed.unlink()
        catalog.scan_directory(root)
        assert catalog.current_files(root) == []
        assert [item.tag for item in catalog.tags_for_content(content_id)] == ["remember"]
        assert catalog.add_content_tag(content_id, "orphaned", provenance) is True
        assert [item.tag for item in catalog.tags_for_content(content_id)] == ["orphaned", "remember"]


def test_producer_versions_coexist_and_retraction_preserves_assertion_history(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "entry.bin").write_bytes(b"entry")
    user = TagProvenance("user", "manual-tool", "1")
    system_v1 = TagProvenance("system", "kind-detector", "1", "rules=a")
    system_v2 = TagProvenance("system", "kind-detector", "2", "rules=b")
    database_path = tmp_path / "catalog.sqlite"

    with Catalog.create(database_path) as catalog:
        catalog.scan_directory(root)
        content_id = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        assert catalog.add_content_tag(content_id, "text", user) is True
        assert catalog.add_content_tag(content_id, "text", system_v1) is True
        assert catalog.add_content_tag(content_id, "text", system_v2) is True
        assert catalog.add_content_tag(content_id, "text", system_v1) is False
        assert [item.provenance for item in catalog.tags_for_content(content_id)] == [system_v1, system_v2, user]

        scan_history_before = list(catalog.scan_history())
        observation_history_before = list(catalog.observation_history())
        assert catalog.retract_content_tag(content_id, "text", system_v1) == 1
        assert catalog.retract_content_tag(content_id, "text", system_v1) == 0
        assert [item.provenance for item in catalog.tags_for_content(content_id)] == [system_v2, user]
        assert catalog.retract_content_tag(content_id, "text") == 1
        assert [item.provenance for item in catalog.tags_for_content(content_id)] == [system_v2]
        assert list(catalog.scan_history()) == scan_history_before
        assert list(catalog.observation_history()) == observation_history_before
        assert catalog.add_content_tag(content_id, "text", system_v1) is True

    connection = sqlite3.connect(database_path)
    history = connection.execute(
        """
        SELECT provenance_kind, source_version, retracted_at_ns
        FROM content_tag_assertions
        ORDER BY id
        """
    ).fetchall()
    connection.close()
    assert len(history) == 4
    assert history[1][2] is not None
    assert history[3] == ("system", "1", None)


def test_reverse_tag_lookup_is_bounded_deterministic_and_filterable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for name, data in (("a.bin", b"a"), ("b.bin", b"b"), ("c.bin", b"c")):
        (root / name).write_bytes(data)
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "4")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        content_ids = [catalog.content_for_path(root, PurePosixPath(name)) for name in ("a.bin", "b.bin", "c.bin")]
        for content_id in content_ids:
            catalog.add_content_tag(content_id, "selected", user)
        catalog.add_content_tag(content_ids[1], "selected", system)

        result = catalog.search_tagged_content("selected", limit=2)
        system_result = catalog.search_tagged_content("selected", provenance="system", limit=20)

    assert result.total_matches == 3
    assert len(result.contents) == 2
    assert [item.content_id.digest for item in result.contents] == sorted(item.digest for item in content_ids)[:2]
    assert system_result.total_matches == 1
    assert system_result.contents[0].content_id == content_ids[1]
    assert {assertion.provenance.kind for assertion in system_result.contents[0].assertions} == {"system"}


def test_tag_operations_reject_unknown_content_invalid_names_and_unsafe_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "entry").write_bytes(b"entry")
    provenance = TagProvenance("user", "manual", "1")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        with pytest.raises(TaggingError, match="content is not known"):
            catalog.add_content_tag(ContentId("sha256", "0" * 64), "valid", provenance)
        with pytest.raises(ValueError, match="tag must match"):
            catalog.add_tag_for_path(root, PurePosixPath("entry"), "Invalid Tag", provenance)
        with pytest.raises(ValueError, match="relative path"):
            catalog.tags_for_path(root, PurePosixPath("../entry"))
        with pytest.raises(TaggingError, match="no current path"):
            catalog.tags_for_path(root, PurePosixPath("missing"))
        with pytest.raises(ValueError, match="limit"):
            catalog.search_tagged_content("valid", limit=0)


@pytest.mark.parametrize(
    ("kind", "name", "tool_version", "detail"),
    (
        ("other", "tool", "1", ""),
        ("system", "", "1", ""),
        ("system", "tool", "", ""),
        ("system", "tool", "1", "bad\nvalue"),
    ),
)
def test_tag_provenance_rejects_invalid_values(kind: str, name: str, tool_version: str, detail: str) -> None:
    with pytest.raises(ValueError):
        TagProvenance(kind, name, tool_version, detail)  # type: ignore[arg-type]
