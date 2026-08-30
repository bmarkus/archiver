from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, TagProvenance
from archiver.cli import main


def _initialize_scan_and_tag(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / "first.bin").write_bytes(b"first")
    (root / "second.bin").write_bytes(b"second")
    (root / "third.bin").write_bytes(b"third")
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "scan", str(root), "--no-progress"]) == 0
    capsys.readouterr()

    user = TagProvenance("user", "manual", "1")
    system = TagProvenance("system", "classifier", "2")
    system_v2 = TagProvenance("system", "classifier", "3")
    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        first = catalog.content_for_path(root, PurePosixPath("first.bin"))
        second = catalog.content_for_path(root, PurePosixPath("second.bin"))
        third = catalog.content_for_path(root, PurePosixPath("third.bin"))
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


def test_available_tags_cli_renders_bounded_counts_filters_and_effective_options(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _initialize_scan_and_tag(root, capsys)

    assert (
        main(
            [
                "catalog",
                "tags",
                "available",
                str(root),
                "--regex",
                "a",
                "--sort",
                "assertions",
                "--limit",
                "2",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Available tags" in output
    assert f"Root: {root.resolve()}" in output
    assert "Sort: assertions (descending)" in output
    assert "Provenance: all" in output
    assert "Regex: a" in output
    assert "Tag" in output
    assert "Content" in output
    assert "Assertions" in output
    assert "User" in output
    assert "System" in output
    assert "alpha" in output
    assert "gamma" in output
    assert "beta" not in output
    assert "retired" not in output
    alpha_columns = [
        column.strip() for column in next(line for line in output.splitlines() if "alpha" in line).split("|")
    ]
    assert alpha_columns == ["alpha", "2", "3", "2", "1"]
    assert "Matched: 3 active tags (showing first 2)" in output

    assert (
        main(
            [
                "catalog",
                "tags",
                "available",
                str(root),
                "--provenance",
                "user",
                "--sort",
                "name",
                "--reverse",
            ]
        )
        == 0
    )
    user_output = capsys.readouterr().out
    assert "Sort: name (descending)" in user_output
    assert "Provenance: user" in user_output
    assert user_output.index("gamma") < user_output.index("beta") < user_output.index("alpha")
    assert "Matched: 3 active tags (showing all 3)" in user_output


def test_available_tags_cli_empty_result_and_fallback_width(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _initialize_scan_and_tag(root, capsys)
    monkeypatch.setattr(
        "archiver.cli.shutil.get_terminal_size",
        lambda fallback=(120, 24): os.terminal_size(fallback),
    )

    assert main(["catalog", "tags", "available", str(root), "--regex", "missing"]) == 0
    empty_output = capsys.readouterr().out
    assert "No matching active tags." in empty_output
    assert "Matched: 0 active tags (showing all 0)" in empty_output

    assert main(["catalog", "tags", "available", str(root)]) == 0
    table_output = capsys.readouterr().out
    table_lines = [line for line in table_output.splitlines() if " | " in line]
    assert table_lines
    assert all(len(line) <= 120 for line in table_lines)


def test_available_tags_cli_rejects_invalid_regex_before_catalog_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_paths_called = False

    def fail_catalog_paths(_root: Path) -> tuple[Path, Path]:
        nonlocal catalog_paths_called
        catalog_paths_called = True
        raise AssertionError("catalog paths must not be resolved")

    monkeypatch.setattr("archiver.cli._catalog_paths", fail_catalog_paths)
    with pytest.raises(SystemExit) as raised:
        main(["catalog", "tags", "available", "missing-root", "--regex", "["])
    assert raised.value.code == 2
    assert catalog_paths_called is False


@pytest.mark.parametrize(
    "arguments",
    (
        ["catalog", "tags", "available", "ROOT", "--limit", "0"],
        ["catalog", "tags", "available", "ROOT", "--sort", "other"],
        ["catalog", "tags", "available", "ROOT", "--provenance", "other"],
    ),
)
def test_available_tags_cli_rejects_invalid_arguments(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(arguments)
    assert raised.value.code == 2


def test_available_tags_cli_is_read_only_for_sources_and_catalog_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _initialize_scan_and_tag(root, capsys)
    source = root / "first.bin"
    database_path = root / ".archiver" / "catalog.sqlite"

    with Catalog.open(database_path) as catalog:
        content_id = catalog.content_for_path(root, PurePosixPath("first.bin"))
        before_catalog = (
            list(catalog.scan_history()),
            list(catalog.observation_history()),
            catalog.tags_for_content(content_id),
            catalog.schema_version,
        )
    before_source = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)

    assert main(["catalog", "tags", "available", str(root), "--regex", "(?i)^ALP"]) == 0
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
