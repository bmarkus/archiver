from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from archiver import Catalog, ContentId, CurrentFileQuery
from archiver._query import CurrentObservationQuery
from archiver._sqlite_query import (
    compile_current_observation_count,
    compile_current_observation_distinct_content_count,
    compile_current_observation_total_size,
    compile_current_observations,
)
from archiver.models import Location
from archiver.query import (
    ContentPredicate,
    PathGlobPredicate,
    SortExpression,
    TagPredicate,
    content,
    mtime,
    path,
    size,
    tags,
)


def test_public_query_objects_are_immutable_semantic_values(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    first_catalog = Catalog.create(tmp_path / "first.sqlite")
    second_catalog = Catalog.create(tmp_path / "second.sqlite")
    try:
        base = first_catalog.files(root)
        predicate = path.glob("nested/*.txt")
        ordering = size.desc()
        derived = base.where(predicate).sort(ordering).limit(7)
        same_value = second_catalog.files(root).where(predicate).sort(ordering).limit(7)

        assert isinstance(base, CurrentFileQuery)
        assert base.predicates == ()
        assert base.ordering == path.asc()
        assert base.row_limit == 20
        assert derived.predicates == (predicate,)
        assert derived.ordering == ordering
        assert derived.row_limit == 7
        assert derived == same_value
        assert "Catalog" not in repr(derived)
        assert tuple(item.name for item in fields(predicate)) == ("field", "pattern")
        assert tuple(item.name for item in fields(ordering)) == ("field", "direction")
        assert predicate.field.value == "path"
        assert ordering.field.value == "size"
        assert ordering.direction.value == "descending"
        assert "relative_path" not in repr((path, size, mtime, predicate, ordering))
        assert "size_bytes" not in repr((path, size, mtime, predicate, ordering))
        assert "mtime_ns" not in repr((path, size, mtime, predicate, ordering))

        with pytest.raises(FrozenInstanceError):
            setattr(derived, "row_limit", 99)
    finally:
        first_catalog.close()
        second_catalog.close()


def test_content_and_tag_predicates_are_canonical_immutable_values() -> None:
    content_id = ContentId("sha256", "a" * 64)
    exact = content.is_(content_id)
    all_tags = tags.all_of("zeta", "alpha", "zeta", provenance="user")
    any_tags = tags.any_of("zeta", "alpha")

    assert isinstance(exact, ContentPredicate)
    assert exact.content_id == content_id
    assert isinstance(all_tags, TagPredicate)
    assert all_tags.names == ("alpha", "zeta")
    assert all_tags.provenance == "user"
    assert all_tags.match.value == "all"
    assert any_tags.names == ("alpha", "zeta")
    assert any_tags.match.value == "any"
    assert all_tags == tags.all_of("alpha", "zeta", provenance="user")

    with pytest.raises(FrozenInstanceError):
        setattr(all_tags, "names", ("changed",))


def test_public_query_rejects_invalid_limits_and_expressions(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        query = catalog.files(root)

        for invalid_limit in (0, -1):
            with pytest.raises(ValueError, match="at least 1"):
                query.limit(invalid_limit)
        for invalid_type in (True, 1.5, "2"):
            with pytest.raises(TypeError, match="integer"):
                query.limit(invalid_type)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="current-file predicate"):
            query.where(object())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="current-file predicate"):
            query.where(size.asc())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="sort expression"):
            query.sort(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="path field"):
            PathGlobPredicate(field=size.token, pattern="*")
        with pytest.raises(TypeError, match="pattern must be a string"):
            path.glob(3)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="sort field"):
            SortExpression(field="size_bytes", direction=size.desc().direction)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="sort direction"):
            SortExpression(field=size.token, direction="DESC")  # type: ignore[arg-type]


def test_content_and_tag_factories_reject_invalid_arguments(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        query = catalog.files(root)
        with pytest.raises(TypeError, match="ContentId"):
            content.is_("sha256:a")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="at least one tag"):
            tags.all_of()
        with pytest.raises(ValueError, match="at least one tag"):
            tags.any_of()
        with pytest.raises(ValueError, match="tag must match"):
            tags.all_of("Invalid Tag")
        with pytest.raises(TypeError, match="tag names must be strings"):
            tags.any_of("valid", 3)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="string or None"):
            tags.all_of("valid", provenance=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="'user', 'system', or None"):
            tags.any_of("valid", provenance="external")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="current-file predicate"):
            query.where(object())  # type: ignore[arg-type]


def test_compiler_maps_closed_tokens_and_parameterizes_predicates_and_limit(tmp_path: Path) -> None:
    location = Location(id=42, root=tmp_path / "source")
    query = CurrentObservationQuery(
        location=location,
        predicates=(path.glob("nested/*.txt'; DROP TABLE content; --"), path.glob("*report*")),
        ordering=mtime.desc(),
        limit=5,
    )

    compiled = compile_current_observations(query)

    assert compiled.parameters == (
        42,
        "nested/*.txt'; DROP TABLE content; --",
        "*report*",
        5,
    )
    assert compiled.statement.count("?") == 4
    assert "DROP TABLE" not in compiled.statement
    assert compiled.statement.count("observations.relative_path GLOB ?") == 2
    assert "ORDER BY observations.mtime_ns DESC, observations.relative_path ASC" in compiled.statement
    assert "LIMIT ?" in compiled.statement


def test_compiler_parameterizes_content_and_distinct_active_tag_predicates(tmp_path: Path) -> None:
    location = Location(id=42, root=tmp_path / "source")
    content_id = ContentId("sha256", "b" * 64)
    query = CurrentObservationQuery(
        location=location,
        predicates=(
            content.is_(content_id),
            tags.all_of("zeta", "alpha", provenance="system"),
            tags.any_of("beta", "alpha"),
        ),
        ordering=path.asc(),
        limit=3,
    )

    compiled = compile_current_observations(query)

    assert compiled.parameters == (
        42,
        "sha256",
        "b" * 64,
        "alpha",
        "zeta",
        "system",
        2,
        "alpha",
        "beta",
        1,
        3,
    )
    assert compiled.statement.count("EXISTS (") == 1
    assert compiled.statement.count("observations.content_id IN (") == 2
    assert "COUNT(DISTINCT matching_tags.name) >= ?" in compiled.statement
    assert "matching_assertions.retracted_at_ns IS NULL" in compiled.statement
    assert "LIMIT ?" in compiled.statement


def test_path_order_does_not_add_a_conflicting_tie_breaker(tmp_path: Path) -> None:
    location = Location(id=7, root=tmp_path / "source")

    compiled = compile_current_observations(CurrentObservationQuery(location=location, ordering=path.desc(), limit=3))

    assert "ORDER BY observations.relative_path DESC" in compiled.statement
    assert "relative_path ASC" not in compiled.statement


def test_reduction_compilers_ignore_row_order_and_limit(tmp_path: Path) -> None:
    location = Location(id=42, root=tmp_path / "source")
    query = CurrentObservationQuery(
        location=location,
        predicates=(path.glob("nested/*.txt"),),
        ordering=mtime.desc(),
        limit=5,
    )

    count = compile_current_observation_count(query)
    total_size = compile_current_observation_total_size(query)
    distinct_content = compile_current_observation_distinct_content_count(query)

    for compiled in (count, total_size, distinct_content):
        assert compiled.parameters == (42, "nested/*.txt")
        assert "ORDER BY" not in compiled.statement
        assert "LIMIT" not in compiled.statement
        assert "observations.relative_path GLOB ?" in compiled.statement
    assert "COUNT(*) AS count" in count.statement
    assert "COALESCE(SUM(observations.size_bytes), 0) AS total_size_bytes" in total_size.statement
    assert "COUNT(DISTINCT observations.content_id) AS distinct_content_count" in distinct_content.statement
