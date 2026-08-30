from __future__ import annotations

import re
import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from archiver import AvailableTagSearch, Catalog, ContentId, TagProvenance


def _seed_available_tags(catalog: Catalog, root: Path) -> tuple[ContentId, ...]:
    catalog.scan_directory(root)
    first = catalog.content_for_path(root, PurePosixPath("first.bin"))
    second = catalog.content_for_path(root, PurePosixPath("second.bin"))
    third = catalog.content_for_path(root, PurePosixPath("third.bin"))
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "2")
    system_v2 = TagProvenance("system", "classifier", "3")

    catalog.add_content_tag(first, "alpha", user)
    catalog.add_content_tag(first, "alpha", system)
    catalog.add_content_tag(second, "alpha", user)
    catalog.add_content_tag(first, "beta", user)
    catalog.add_content_tag(second, "beta", system)
    catalog.add_content_tag(third, "gamma", user)
    catalog.add_content_tag(third, "gamma", system)
    catalog.add_content_tag(third, "gamma", system_v2)
    catalog.add_content_tag(third, "retired", user)
    catalog.retract_content_tag(third, "retired", user)
    return first, second, third


def test_available_tags_counts_active_usage_across_orphaned_content_and_provenance(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "first.bin").write_bytes(b"first")
    second_path = root / "second.bin"
    second_path.write_bytes(b"second")
    (root / "third.bin").write_bytes(b"third")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        _seed_available_tags(catalog, root)
        second_path.unlink()
        catalog.scan_directory(root)

        result = catalog.search_available_tags()
        user_result = catalog.search_available_tags(provenance="user")
        system_result = catalog.search_available_tags(provenance="system")

    assert isinstance(result, AvailableTagSearch)
    assert result.total_matches == 3
    assert [usage.tag for usage in result.tags] == ["alpha", "beta", "gamma"]
    assert result.tags[0].content_count == 2
    assert result.tags[0].assertion_count == 3
    assert result.tags[0].user_assertion_count == 2
    assert result.tags[0].system_assertion_count == 1
    assert result.tags[1].content_count == 2
    assert result.tags[1].assertion_count == 2
    assert result.tags[2].content_count == 1
    assert result.tags[2].assertion_count == 3

    assert [usage.tag for usage in user_result.tags] == ["alpha", "beta", "gamma"]
    assert user_result.tags[0].content_count == 2
    assert user_result.tags[0].assertion_count == 2
    assert user_result.tags[0].user_assertion_count == 2
    assert user_result.tags[0].system_assertion_count == 0
    assert [usage.tag for usage in system_result.tags] == ["alpha", "beta", "gamma"]
    assert system_result.tags[0].content_count == 1
    assert system_result.tags[0].assertion_count == 1
    assert system_result.tags[0].user_assertion_count == 0
    assert system_result.tags[0].system_assertion_count == 1


def test_available_tags_regex_sorting_totals_and_sql_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for name in ("first", "second", "third"):
        (root / f"{name}.bin").write_bytes(name.encode())

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        _seed_available_tags(catalog, root)
        statements: list[str] = []
        catalog._connection.set_trace_callback(statements.append)
        try:
            bounded = catalog.search_available_tags(sort_by="assertions", limit=2)
        finally:
            catalog._connection.set_trace_callback(None)
        by_content = catalog.search_available_tags(sort_by="content")
        content_reverse = catalog.search_available_tags(sort_by="content", reverse=True)
        assertion_reverse = catalog.search_available_tags(sort_by="assertions", reverse=True)
        name_reverse = catalog.search_available_tags(sort_by="name", reverse=True)
        substring = catalog.search_available_tags(name_regex="ph")
        insensitive = catalog.search_available_tags(name_regex="(?i)^ALP")
        no_match = catalog.search_available_tags(name_regex="missing")

    assert bounded.total_matches == 3
    assert [usage.tag for usage in bounded.tags] == ["alpha", "gamma"]
    assert "LIMIT 2" in "\n".join(statements).upper()
    assert [usage.tag for usage in by_content.tags] == ["alpha", "beta", "gamma"]
    assert [usage.tag for usage in content_reverse.tags] == ["gamma", "alpha", "beta"]
    assert [usage.tag for usage in assertion_reverse.tags] == ["beta", "alpha", "gamma"]
    assert [usage.tag for usage in name_reverse.tags] == ["gamma", "beta", "alpha"]
    assert [usage.tag for usage in substring.tags] == ["alpha"]
    assert [usage.tag for usage in insensitive.tags] == ["alpha"]
    assert no_match == AvailableTagSearch(tags=(), total_matches=0)


def test_available_tags_validates_before_sql_is_read_only_and_cleans_up_regex_function(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "first.bin"
    source.write_bytes(b"first")
    (root / "second.bin").write_bytes(b"second")
    (root / "third.bin").write_bytes(b"third")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        content_ids = _seed_available_tags(catalog, root)
        before = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            tuple(catalog.tags_for_content(content_id) for content_id in content_ids),
            catalog.schema_version,
            source.read_bytes(),
            source.stat().st_mtime_ns,
            source.stat().st_mode,
        )
        statements: list[str] = []
        catalog._connection.set_trace_callback(statements.append)
        try:
            with pytest.raises(re.error):
                catalog.search_available_tags(name_regex="[")
        finally:
            catalog._connection.set_trace_callback(None)
        assert statements == []

        with pytest.raises(ValueError, match="provenance must"):
            catalog.search_available_tags(provenance="other")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="unsupported available-tag sort"):
            catalog.search_available_tags(sort_by="other")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="limit must"):
            catalog.search_available_tags(limit=0)

        catalog.search_available_tags(name_regex="alpha")
        with pytest.raises(sqlite3.OperationalError):
            catalog._connection.execute("SELECT archiver_tag_name_regex('alpha')").fetchone()

        after = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            tuple(catalog.tags_for_content(content_id) for content_id in content_ids),
            catalog.schema_version,
            source.read_bytes(),
            source.stat().st_mtime_ns,
            source.stat().st_mode,
        )

    assert after == before


def test_available_tags_cleans_up_regex_function_after_query_failure(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for name in ("first", "second", "third"):
        (root / f"{name}.bin").write_bytes(name.encode())

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        _seed_available_tags(catalog, root)
        catalog._connection.set_authorizer(
            lambda action, _one, _two, _database, _trigger: (
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK
            )
        )
        try:
            with pytest.raises(sqlite3.DatabaseError):
                catalog.search_available_tags(name_regex="alpha")
        finally:
            catalog._connection.set_authorizer(None)
        with pytest.raises(sqlite3.OperationalError):
            catalog._connection.execute("SELECT archiver_tag_name_regex('alpha')").fetchone()
