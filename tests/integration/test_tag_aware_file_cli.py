from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, TagProvenance
from archiver.cli import main


def _initialize_and_scan(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "scan", str(root), "--no-progress"]) == 0
    capsys.readouterr()


def _tag_fixture(root: Path) -> None:
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "2")
    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        shared = catalog.content_for_path(root, PurePosixPath("a-copy.bin"))
        second = catalog.content_for_path(root, PurePosixPath("b.bin"))
        for tag in ("alpha", "family"):
            catalog.add_content_tag(shared, tag, user)
        for tag in ("family", "trip:us"):
            catalog.add_content_tag(shared, tag, system)
            catalog.add_content_tag(second, tag, user)


def test_catalog_files_tag_options_filter_compose_and_preserve_complete_totals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a-copy.bin").write_bytes(b"shared")
    (root / "nested").mkdir()
    (root / "nested" / "a.bin").write_bytes(b"shared")
    (root / "b.bin").write_bytes(b"second")
    (root / "untagged.bin").write_bytes(b"plain")
    _initialize_and_scan(root, capsys)
    _tag_fixture(root)

    assert (
        main(
            [
                "catalog",
                "files",
                str(root),
                "--tag",
                "family",
                "--tag",
                "trip:us",
                "--limit",
                "2",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Modified (UTC)" in output
    assert "SHA-256" in output
    assert "Path" in output
    assert "Tags" in output
    assert "alpha, family, trip:us" in output
    assert "Matched: 3 files · 2 distinct contents · 18 B across file paths (showing first 2)" in output
    assert "Required tags: family AND trip:us" in output
    assert "Provenance: all" in output

    assert (
        main(
            [
                "catalog",
                "files",
                str(root),
                "--tag",
                "alpha",
                "--tag",
                "trip:us",
                "--match-any-tag",
            ]
        )
        == 0
    )
    any_output = capsys.readouterr().out
    assert "Matched: 3 files · 2 distinct contents · 18 B across file paths (showing all 3)" in any_output
    assert "Required tags: alpha OR trip:us" in any_output

    assert (
        main(
            [
                "catalog",
                "files",
                str(root),
                "--path",
                "nested/*",
                "--tag",
                "family",
                "--tag",
                "trip:us",
                "--provenance",
                "system",
            ]
        )
        == 0
    )
    filtered_output = capsys.readouterr().out
    assert "nested/a.bin" in filtered_output
    assert "a-copy.bin" not in filtered_output
    assert "Matched: 1 file · 1 distinct content · 6 B across file paths (showing all 1)" in filtered_output
    assert "Provenance: system" in filtered_output


def test_catalog_files_tag_display_handles_overflow_all_tags_empty_and_narrow_width(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    long_path = "a-very-long-path-name-that-must-be-truncated-before-tags.bin"
    (root / long_path).write_bytes(b"shared")
    _initialize_and_scan(root, capsys)
    provenance = TagProvenance("user", "manual", "1")
    tag_names = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        content_id = catalog.content_for_path(root, PurePosixPath(long_path))
        for tag in tag_names:
            catalog.add_content_tag(content_id, tag, provenance)

    monkeypatch.setattr(
        "archiver.cli.shutil.get_terminal_size",
        lambda fallback=(120, 24): os.terminal_size((70, 24)),
    )
    assert main(["catalog", "files", str(root), "--show-tags", "--display-tag-limit", "2"]) == 0
    preview_output = capsys.readouterr().out
    assert "alpha, bravo +4" in preview_output
    assert long_path not in preview_output
    assert "a-very-long-path-na…" in preview_output

    assert main(["catalog", "files", str(root), "--tag", "alpha", "--all-tags"]) == 0
    all_output = capsys.readouterr().out
    for tag in tag_names:
        assert tag in all_output
    assert "+4" not in all_output
    assert all_output.count(" | ") > 8

    assert main(["catalog", "files", str(root), "--tag", "absent"]) == 0
    empty_output = capsys.readouterr().out
    assert "No matching files." in empty_output
    assert "Matched: 0 files · 0 distinct contents · 0 B across file paths (showing all 0)" in empty_output
    assert "Required tags: absent" in empty_output


def test_catalog_files_without_tag_options_uses_legacy_path_exactly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "entry.bin").write_bytes(b"entry")
    _initialize_and_scan(root, capsys)

    def unexpected_tag_query(*args: object, **kwargs: object) -> object:
        raise AssertionError("legacy command dispatched through tag-aware query")

    monkeypatch.setattr(Catalog, "search_current_files_with_tags", unexpected_tag_query)
    assert main(["catalog", "files", str(root), "--sort", "size", "--reverse"]) == 0
    output = capsys.readouterr().out

    assert "Sort: size (ascending)" in output
    assert "entry.bin" in output
    assert "Tags" not in output
    assert "Required tags:" not in output
    assert "Matched: 1 files · 5 B total (showing all 1)" in output


@pytest.mark.parametrize(
    "arguments",
    (
        ["catalog", "files", "ROOT", "--match-any-tag"],
        ["catalog", "files", "ROOT", "--provenance", "user"],
        ["catalog", "files", "ROOT", "--display-tag-limit", "0"],
        ["catalog", "files", "ROOT", "--display-tag-limit", "2", "--all-tags"],
        ["catalog", "files", "ROOT", "--tag", "Invalid Tag"],
    ),
)
def test_catalog_files_rejects_invalid_tag_option_combinations(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(arguments)
    assert raised.value.code == 2


def test_catalog_files_tag_aware_cli_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.bin"
    source.write_bytes(b"entry")
    _initialize_and_scan(root, capsys)
    provenance = TagProvenance("user", "manual", "1")
    database_path = root / ".archiver" / "catalog.sqlite"
    with Catalog.open(database_path) as catalog:
        content_id = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        catalog.add_content_tag(content_id, "stable", provenance)
        before_catalog = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            catalog.tags_for_content(content_id),
            catalog.schema_version,
        )
    before_source = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)

    assert main(["catalog", "files", str(root), "--tag", "stable", "--all-tags"]) == 0
    capsys.readouterr()

    with Catalog.open(database_path) as catalog:
        after_catalog = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            catalog.tags_for_content(content_id),
            catalog.schema_version,
        )
    after_source = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)
    assert after_catalog == before_catalog
    assert after_source == before_source
