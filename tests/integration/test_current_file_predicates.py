from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, ContentId, FileObservation, ScanFailure, TagProvenance
from archiver._query import CurrentObservationQuery
from archiver._sqlite_query import SqliteQuery
from archiver.query import content, path, size, tags


def _paths(rows: Iterable[FileObservation]) -> list[str]:
    return [row.relative_path.as_posix() for row in rows]


def _content_for(catalog: Catalog, root: Path, relative_path: str) -> ContentId:
    return catalog.content_for_path(root, PurePosixPath(relative_path))


def test_content_and_tag_predicates_compose_across_every_terminal(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "nested").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha")
    (root / "nested" / "alpha-copy.txt").write_bytes(b"alpha")
    (root / "beta.txt").write_bytes(b"beta")
    (root / "empty.txt").write_bytes(b"")
    (root / "untagged.bin").write_bytes(b"untagged")
    source_paths = tuple(path for path in root.rglob("*") if path.is_file())
    source_before = {item: (item.read_bytes(), item.stat().st_mtime_ns, item.stat().st_mode) for item in source_paths}
    user = TagProvenance("user", "manual", "1")
    second_user = TagProvenance("user", "other-manual", "1")
    system = TagProvenance("system", "classifier", "2")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        alpha = _content_for(catalog, root, "alpha.txt")
        beta = _content_for(catalog, root, "beta.txt")
        empty = _content_for(catalog, root, "empty.txt")
        catalog.add_content_tag(alpha, "red", user)
        catalog.add_content_tag(alpha, "red", system)
        catalog.add_content_tag(alpha, "red", second_user)
        catalog.add_content_tag(alpha, "blue", user)
        catalog.add_content_tag(beta, "blue", system)
        catalog.add_content_tag(beta, "green", user)
        catalog.retract_content_tag(beta, "green", user)
        catalog.add_content_tag(empty, "red", system)
        expected_history = list(catalog.observation_history())
        expected_scan = catalog.current_scan(root)
        expected_schema = catalog.schema_version

        exact = catalog.files(root).where(content.is_(alpha)).sort(size.desc()).limit(1)
        assert _paths(exact.fetch()) == ["alpha.txt"]
        assert exact.first() is not None
        assert exact.first().relative_path.as_posix() == "alpha.txt"  # type: ignore[union-attr]
        assert exact.count() == 2
        assert exact.distinct_content_count() == 1
        assert exact.total_size_bytes() == 10
        assert _paths(exact.stream()) == ["alpha.txt"]
        assert _paths(exact.stream_all()) == ["alpha.txt", "nested/alpha-copy.txt"]

        all_tags = catalog.files(root).where(tags.all_of("red", "blue")).limit(1)
        assert _paths(all_tags.fetch()) == ["alpha.txt"]
        assert all_tags.count() == 2
        assert all_tags.distinct_content_count() == 1
        assert all_tags.total_size_bytes() == 10

        assert catalog.files(root).where(tags.all_of("red", "blue", provenance="user")).count() == 2
        assert catalog.files(root).where(tags.all_of("red", "blue", provenance="system")).count() == 0
        assert _paths(catalog.files(root).where(tags.any_of("blue", provenance="system")).fetch()) == ["beta.txt"]
        assert catalog.files(root).where(tags.any_of("green", provenance="user")).count() == 0
        assert _paths(catalog.files(root).where(tags.any_of("red", provenance="system")).fetch()) == [
            "alpha.txt",
            "empty.txt",
            "nested/alpha-copy.txt",
        ]

        composed = catalog.files(root).where(tags.any_of("red", "blue")).where(path.glob("nested/*"))
        assert _paths(composed.fetch()) == ["nested/alpha-copy.txt"]
        fully_composed = (
            catalog.files(root)
            .where(content.is_(alpha))
            .where(tags.any_of("red"))
            .where(tags.any_of("blue"))
            .where(path.glob("nested/*"))
        )
        assert _paths(fully_composed.fetch()) == ["nested/alpha-copy.txt"]
        assert catalog.files(root).where(content.is_(ContentId("sha256", "0" * 64))).count() == 0
        assert list(catalog.observation_history()) == expected_history
        assert catalog.current_scan(root) == expected_scan
        assert catalog.schema_version == expected_schema

    assert {
        item: (item.read_bytes(), item.stat().st_mtime_ns, item.stat().st_mode) for item in source_paths
    } == source_before


def test_predicates_are_lazy_track_current_state_and_require_open_catalog(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry.txt"
    entry.write_bytes(b"stable")
    user = TagProvenance("user", "manual", "1")

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        query = catalog.files(root).where(tags.any_of("later"))
        assert query.count() == 0
        assert query.distinct_content_count() == 0

        catalog.scan_directory(root)
        stable = _content_for(catalog, root, "entry.txt")
        exact = catalog.files(root).where(content.is_(stable))
        assert exact.distinct_content_count() == 1
        assert query.count() == 0

        catalog.add_content_tag(stable, "later", user)
        assert query.count() == 1
        assert query.distinct_content_count() == 1
        catalog.retract_content_tag(stable, "later", user)
        assert query.count() == 0

        entry.write_bytes(b"changed")

        def fail(_: object) -> None:
            raise RuntimeError("controlled callback failure")

        with pytest.raises(ScanFailure, match="reconciliation failed"):
            catalog.scan_directory(root, progress_callback=fail)
        assert exact.count() == 1
        assert exact.distinct_content_count() == 1

        def interrupt(_: object) -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            catalog.scan_directory(root, progress_callback=interrupt)
        assert exact.count() == 1
        assert exact.distinct_content_count() == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        exact.distinct_content_count()


def test_distinct_content_reduction_failure_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    entry = root / "entry.txt"
    entry.write_bytes(b"stable")
    source_before = (entry.read_bytes(), entry.stat().st_mtime_ns, entry.stat().st_mode)

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        query = catalog.files(root)
        expected_history = list(catalog.observation_history())
        expected_scan = catalog.current_scan(root)
        expected_schema = catalog.schema_version

        def compile_invalid(_: CurrentObservationQuery) -> SqliteQuery:
            return SqliteQuery("SELECT * FROM missing_current_observations", ())

        with monkeypatch.context() as patcher:
            patcher.setattr(
                "archiver._sqlite_query.compile_current_observation_distinct_content_count", compile_invalid
            )
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                query.distinct_content_count()

        assert query.distinct_content_count() == 1
        assert list(catalog.observation_history()) == expected_history
        assert catalog.current_scan(root) == expected_scan
        assert catalog.schema_version == expected_schema
        assert (entry.read_bytes(), entry.stat().st_mtime_ns, entry.stat().st_mode) == source_before
