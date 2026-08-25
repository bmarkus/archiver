from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, TagProvenance
from archiver.cli import main


def _initialize_and_scan(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "scan", str(root)]) == 0
    capsys.readouterr()


def test_tags_find_cli_supports_multi_tag_matching_details_totals_and_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.bin").write_bytes(b"shared")
    (root / "a-copy.bin").write_bytes(b"shared")
    (root / "b.bin").write_bytes(b"second")
    _initialize_and_scan(root, capsys)
    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "2")

    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        shared = catalog.content_for_path(root, PurePosixPath("a.bin"))
        second = catalog.content_for_path(root, PurePosixPath("b.bin"))
        for tag in ("alpha", "family", "trip:us"):
            catalog.add_content_tag(shared, tag, user)
        catalog.add_content_tag(shared, "family", system)
        catalog.add_content_tag(shared, "trip:us", system)
        catalog.add_content_tag(second, "family", user)

    assert (
        main(
            [
                "catalog",
                "tags",
                "find",
                str(root),
                "family",
                "trip:us",
                "family",
                "--details",
                "--path-limit",
                "1",
                "--display-tag-limit",
                "2",
            ]
        )
        == 0
    )
    all_output = capsys.readouterr().out
    assert "SHA-256" in all_output
    assert "Size" in all_output
    assert "Paths" in all_output
    assert "Tags" in all_output
    assert "alpha, family +1" in all_output
    assert "a-copy.bin" in all_output
    assert "\n  a.bin\n" not in all_output
    assert "Matched: 1 content identity" in all_output
    assert "2 current paths (showing all 1)" in all_output
    assert "Required tags: family AND trip:us" in all_output

    assert main(["catalog", "tags", "find", str(root), "family", "trip:us", "--match", "any", "--limit", "1"]) == 0
    any_output = capsys.readouterr().out
    assert "Matched: 2 content identities" in any_output
    assert "3 current paths (showing first 1)" in any_output
    assert "Required tags: family OR trip:us" in any_output

    assert (
        main(
            [
                "catalog",
                "tags",
                "find",
                str(root),
                "family",
                "trip:us",
                "--provenance",
                "system",
            ]
        )
        == 0
    )
    system_output = capsys.readouterr().out
    assert "Provenance: system" in system_output
    assert "Matched: 1 content identity" in system_output


def test_tags_find_cli_all_tags_wraps_without_omitting_names(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "entry.bin").write_bytes(b"entry")
    _initialize_and_scan(root, capsys)
    provenance = TagProvenance("user", "manual", "1")
    tag_names = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")

    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        content_id = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        for tag in tag_names:
            catalog.add_content_tag(content_id, tag, provenance)

    monkeypatch.setattr("archiver.cli.shutil.get_terminal_size", lambda fallback=(80, 24): os.terminal_size((55, 24)))
    assert main(["catalog", "tags", "find", str(root), "alpha", "--all-tags"]) == 0
    output = capsys.readouterr().out

    for tag in tag_names:
        assert tag in output
    assert "+5" not in output
    assert output.count(" | ") > 6


def test_tags_find_cli_keeps_orphaned_content_and_excludes_retracted_assertions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.bin"
    source.write_bytes(b"entry")
    _initialize_and_scan(root, capsys)
    provenance = TagProvenance("user", "manual", "1")

    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        content_id = catalog.content_for_path(root, PurePosixPath("entry.bin"))
        catalog.add_content_tag(content_id, "remember", provenance)
        catalog.add_content_tag(content_id, "hidden", provenance)
        catalog.retract_content_tag(content_id, "hidden", provenance)
    source.unlink()
    assert main(["catalog", "scan", str(root), "--no-progress"]) == 0
    capsys.readouterr()

    assert main(["catalog", "tags", "find", str(root), "remember", "--details"]) == 0
    remembered_output = capsys.readouterr().out
    assert "Matched: 1 content identity" in remembered_output
    assert "0 current paths" in remembered_output
    assert "remember" in remembered_output

    assert main(["catalog", "tags", "find", str(root), "hidden"]) == 0
    hidden_output = capsys.readouterr().out
    assert "No matching content." in hidden_output
    assert "Matched: 0 content identities" in hidden_output


def test_tags_find_cli_is_read_only_for_sources_and_catalog_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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

    assert main(["catalog", "tags", "find", str(root), "stable", "--details", "--all-tags"]) == 0
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


@pytest.mark.parametrize(
    "arguments",
    (
        ["catalog", "tags", "find", "ROOT"],
        ["catalog", "tags", "find", "ROOT", "valid", "--match", "neither"],
        ["catalog", "tags", "find", "ROOT", "valid", "--path-limit", "0"],
        ["catalog", "tags", "find", "ROOT", "valid", "--display-tag-limit", "0"],
        ["catalog", "tags", "find", "ROOT", "valid", "--display-tag-limit", "2", "--all-tags"],
    ),
)
def test_tags_find_cli_rejects_invalid_arguments(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(arguments)
    assert raised.value.code == 2
