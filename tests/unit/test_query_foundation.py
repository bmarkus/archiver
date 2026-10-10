from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

import archiver
from archiver._query import CurrentObservationQuery
from archiver._sqlite_query import compile_current_observations
from archiver.models import Location


def test_current_observation_query_is_immutable_and_domain_only(tmp_path: Path) -> None:
    location = Location(id=7, root=tmp_path / "source")
    query = CurrentObservationQuery(location=location)

    assert tuple(field.name for field in fields(query)) == ("location", "scan_id", "predicates", "ordering", "limit")
    assert query.location == location
    assert query.predicates == ()
    assert query.limit is None
    assert not hasattr(query, "statement")
    assert not hasattr(query, "parameters")
    assert not hasattr(archiver, "CurrentObservationQuery")

    with pytest.raises(FrozenInstanceError):
        setattr(query, "location", Location(id=8, root=tmp_path / "other"))


def test_current_observation_query_requires_a_persisted_location(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires a persisted location"):
        CurrentObservationQuery(location=Location(id=None, root=tmp_path / "source"))


def test_current_observation_compiler_emits_parameterized_ordered_sql(tmp_path: Path) -> None:
    root = tmp_path / "source"
    query = CurrentObservationQuery(location=Location(id=42, root=root))

    compiled = compile_current_observations(query)

    assert compiled.parameters == (42,)
    assert compiled.statement.count("?") == 1
    assert str(root) not in compiled.statement
    assert "scans.id = locations.current_scan_id" in compiled.statement
    assert "ORDER BY observations.relative_path ASC" in compiled.statement
