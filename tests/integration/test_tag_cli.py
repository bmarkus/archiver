from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, TagProvenance
from archiver.cli import main


def _initialize_and_scan(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "scan", str(root)]) == 0
    capsys.readouterr()


def test_cli_add_list_find_and_remove_tags_by_path_and_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"duplicates")
    second.write_bytes(b"duplicates")
    _initialize_and_scan(root, capsys)

    assert main(["catalog", "tags", "add", str(root), "favorite", "--path", "first.bin"]) == 0
    output = capsys.readouterr().out
    assert "Tag added" in output

    assert main(["catalog", "tags", "add", str(root), "favorite", "--path", "second.bin"]) == 0
    assert "no catalog change was needed" in capsys.readouterr().out

    assert main(["catalog", "tags", "list", str(root), "--path", "second.bin"]) == 0
    output = capsys.readouterr().out
    assert "favorite  user  archiver-cli@" in output

    database_path = root / ".archiver" / "catalog.sqlite"
    with Catalog.open(database_path) as catalog:
        content_id = catalog.content_for_path(root, PurePosixPath("first.bin"))
        catalog.add_content_tag(content_id, "favorite", TagProvenance("system", "classifier", "2", "rules=v2"))

    assert main(["catalog", "tags", "find", str(root), "favorite", "--provenance", "system"]) == 0
    output = capsys.readouterr().out
    assert "Provenance: system" in output
    assert "Matched: 1 content identity" in output
    assert "2 current paths" in output
    assert "favorite" in output

    assert main(["catalog", "tags", "remove", str(root), "favorite", "--content", content_id.digest]) == 0
    assert "1 user assertion retracted" in capsys.readouterr().out
    assert main(["catalog", "tags", "list", str(root), "--content", content_id.digest]) == 0
    output = capsys.readouterr().out
    assert "system  classifier@2 [rules=v2]" in output
    assert "archiver-cli" not in output


def test_cli_tagging_preserves_source_bytes_and_metadata(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "entry.txt"
    source.write_bytes(b"source")
    _initialize_and_scan(root, capsys)
    before = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)

    assert main(["catalog", "tags", "add", str(root), "text", "--path", "entry.txt"]) == 0
    capsys.readouterr()
    assert main(["catalog", "tags", "list", str(root), "--path", "entry.txt"]) == 0
    capsys.readouterr()
    assert main(["catalog", "tags", "remove", str(root), "text", "--path", "entry.txt"]) == 0
    capsys.readouterr()

    assert (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode) == before


def test_cli_migrate_is_explicit_and_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"source")
    source_before = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    database_path = root / ".archiver" / "catalog.sqlite"
    connection = sqlite3.connect(database_path)
    with connection:
        connection.execute("DROP TABLE content_tag_assertions")
        connection.execute("DROP TABLE tags")
        connection.execute("UPDATE catalog_metadata SET schema_version = 1")
    connection.close()

    assert main(["catalog", "info", str(root)]) == 1
    assert "requires migration" in capsys.readouterr().err
    assert main(["catalog", "migrate", str(root)]) == 0
    assert "Catalog migrated" in capsys.readouterr().out
    assert main(["catalog", "migrate", str(root)]) == 0
    assert "Catalog already current" in capsys.readouterr().out
    assert (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode) == source_before


@pytest.mark.parametrize(
    "arguments",
    (
        ["catalog", "tags", "add", "ROOT", "Upper", "--path", "entry"],
        ["catalog", "tags", "add", "ROOT", "valid", "--content", "abc"],
        ["catalog", "tags", "list", "ROOT", "--path", "../entry"],
        ["catalog", "tags", "find", "ROOT", "valid", "--limit", "0"],
    ),
)
def test_cli_rejects_invalid_tag_arguments(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(arguments)
    assert raised.value.code == 2
