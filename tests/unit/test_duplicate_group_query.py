from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from archiver import Catalog, DuplicateGroupQuery
from archiver._query import DuplicateGroupQueryDescription
from archiver._sqlite_query import (
    compile_duplicate_group_members,
    compile_duplicate_groups,
    compile_duplicate_summary,
)
from archiver.models import Location


def test_public_duplicate_queries_are_immutable_semantic_values(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    first_catalog = Catalog.create(tmp_path / "first.sqlite")
    second_catalog = Catalog.create(tmp_path / "second.sqlite")
    try:
        base = first_catalog.duplicates(root)
        derived = base.limit(7).member_limit(3)
        same_value = second_catalog.duplicates(root).limit(7).member_limit(3)

        assert isinstance(base, DuplicateGroupQuery)
        assert base.root == root.resolve()
        assert base.group_limit == 20
        assert base.member_row_limit == 20
        assert derived.group_limit == 7
        assert derived.member_row_limit == 3
        assert derived == same_value
        assert "Catalog" not in repr(derived)
        with pytest.raises(FrozenInstanceError):
            setattr(derived, "group_limit", 99)
    finally:
        first_catalog.close()
        second_catalog.close()


def test_internal_duplicate_description_requires_resolved_domain_values(tmp_path: Path) -> None:
    root = tmp_path / "source"
    persisted = Location(id=7, root=root)

    with pytest.raises(ValueError, match="persisted location"):
        DuplicateGroupQueryDescription(Location(id=None, root=root), 1, 2, 3)
    with pytest.raises(TypeError, match="scan ID"):
        DuplicateGroupQueryDescription(persisted, True, 2, 3)
    with pytest.raises(ValueError, match="scan ID"):
        DuplicateGroupQueryDescription(persisted, 0, 2, 3)
    with pytest.raises(ValueError, match="at least 1"):
        DuplicateGroupQueryDescription(persisted, 1, 0, 3)
    with pytest.raises(TypeError, match="integer"):
        DuplicateGroupQueryDescription(persisted, 1, 2, 3.5)  # type: ignore[arg-type]


def test_duplicate_compilers_share_one_scan_and_apply_sql_bounds(tmp_path: Path) -> None:
    query = DuplicateGroupQueryDescription(
        location=Location(id=7, root=tmp_path / "source"),
        scan_id=11,
        group_limit=3,
        member_limit=2,
    )

    groups = compile_duplicate_groups(query)
    members = compile_duplicate_group_members(query, 42)
    summary = compile_duplicate_summary(query)

    assert groups.parameters == (11, 3)
    assert members.parameters == (11, 42, 2)
    assert summary.parameters == (11,)
    assert groups.statement.count("?") == 2
    assert members.statement.count("?") == 3
    assert summary.statement.count("?") == 1
    assert "GROUP BY" in groups.statement
    assert "HAVING COUNT(*) > 1" in groups.statement
    assert "potential_redundant_bytes DESC" in groups.statement
    assert "content.algorithm ASC, content.digest ASC" in groups.statement
    assert "LIMIT ?" in groups.statement
    assert "ORDER BY observations.relative_path ASC" in members.statement
    assert "LIMIT ?" in members.statement
    assert "GROUP BY observations.content_id" in summary.statement
    assert "ORDER BY" not in summary.statement
    assert "LIMIT" not in summary.statement


def test_member_compiler_rejects_invalid_private_content_ids(tmp_path: Path) -> None:
    query = DuplicateGroupQueryDescription(Location(id=7, root=tmp_path), 11, 3, 2)

    with pytest.raises(TypeError, match="content ID"):
        compile_duplicate_group_members(query, True)
    with pytest.raises(ValueError, match="content ID"):
        compile_duplicate_group_members(query, 0)
