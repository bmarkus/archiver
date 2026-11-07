"""CLI pagination ranges, compatibility, validation, and refresh boundaries."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from archiver import Catalog, DuplicateGroupQuery, DuplicateGroupView
from archiver.cli import main


def _initialize(root: Path, count: int, capsys: pytest.CaptureFixture[str]) -> Path:
    for index in range(1, count + 1):
        folder = root / f"g{index:03}"
        folder.mkdir()
        for member in range(2):
            (folder / f"copy{member}.bin").write_bytes(bytes([index]) * index)
    assert main(["catalog", "init", str(root)]) == 0
    assert main(["catalog", "refresh", str(root), "--no-progress"]) == 0
    capsys.readouterr()
    return root / ".archiver" / "catalog.sqlite"


def test_cli_middle_last_partial_and_empty_pages_preserve_totals_and_member_bounds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _initialize(tmp_path, 100, capsys)
    before = database.read_bytes()
    sources = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.rglob("*.bin")}
    for offset, size, start, end in [(10, 10, 11, 20), (90, 10, 91, 100), (97, 10, 98, 100)]:
        assert (
            main(
                [
                    "catalog",
                    "duplicates",
                    str(tmp_path),
                    "--details",
                    "--group-offset",
                    str(offset),
                    "--group-limit",
                    str(size),
                    "--member-limit",
                    "1",
                ]
            )
            == 0
        )
        output = capsys.readouterr()
        assert output.err == ""
        assert "Duplicate groups: 100" in output.out
        assert "Duplicate file instances: 200" in output.out
        assert f"Showing groups {start}-{end} of 100 duplicate groups." in output.out
        digest = hashlib.sha256(bytes([101 - start]) * (101 - start)).hexdigest()[:12]
        assert f"{start:<5}  {digest}" in output.out
        assert f"g{101 - start:03}/copy0.bin" in output.out
        assert "copy1.bin" not in output.out
    for offset in (100, 101, 2**63 - 1):
        assert main(["catalog", "duplicates", str(tmp_path), "--details", "--group-offset", str(offset)]) == 0
        empty_output = capsys.readouterr().out
        assert "Duplicate groups: 100" in empty_output
        assert f"No duplicate groups in the requested page (offset {offset})." in empty_output
        assert "Showing groups" not in empty_output
    assert database.read_bytes() == before
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.rglob("*.bin")} == sources


def test_cli_zero_offset_is_identical_and_summary_ignores_pagination(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialize(tmp_path, 3, capsys)
    for options in ([], ["--details"], ["--details", "--group-limit", "1"]):
        arguments = ["catalog", "duplicates", str(tmp_path), *options]
        assert main(arguments) == 0
        original = capsys.readouterr()
        assert main([*arguments, "--group-offset", "0"]) == 0
        assert capsys.readouterr() == original
    arguments = ["catalog", "duplicates", str(tmp_path)]
    assert main(arguments) == 0
    original = capsys.readouterr()
    assert main([*arguments, "--group-offset", "99", "--group-limit", "1"]) == 0
    assert capsys.readouterr() == original


@pytest.mark.parametrize("value", ["-1", str(2**63), "True", "1.5", "None"])
def test_cli_rejects_bad_offsets_before_catalog_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    def forbidden_open(*args: object, **kwargs: object) -> Catalog:
        pytest.fail("invalid offset must be rejected before catalog opening")

    monkeypatch.setattr(Catalog, "open", forbidden_open)
    with pytest.raises(SystemExit) as raised:
        main(["catalog", "duplicates", str(tmp_path), "--group-offset", value])
    assert raised.value.code == 2
    assert not (tmp_path / ".archiver").exists()


def test_cli_help_and_empty_states(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["catalog", "duplicates", "--help"])
    assert raised.value.code == 0
    assert "--group-offset" in capsys.readouterr().out
    assert main(["catalog", "init", str(tmp_path)]) == 0
    capsys.readouterr()
    args = ["catalog", "duplicates", str(tmp_path), "--details", "--group-offset", "10"]
    assert main(args) == 0
    assert "Current scan: none" in capsys.readouterr().out
    assert main(["catalog", "refresh", str(tmp_path), "--no-progress"]) == 0
    capsys.readouterr()
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "Duplicate groups: 0" in output and "requested page (offset 10)" in output


def test_cli_summary_and_fetch_can_resolve_different_successful_scans(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _initialize(tmp_path, 3, capsys)
    original_fetch = DuplicateGroupQuery.fetch

    def refresh_before_fetch(query: DuplicateGroupQuery) -> tuple[DuplicateGroupView, ...]:
        (tmp_path / "g003" / "copy1.bin").unlink()
        with Catalog.open(database) as writer:
            writer.scan_directory(tmp_path, excluded_directories=(tmp_path / ".archiver",))
        return original_fetch(query)

    monkeypatch.setattr(DuplicateGroupQuery, "fetch", refresh_before_fetch)
    assert main(["catalog", "duplicates", str(tmp_path), "--details", "--group-offset", "1"]) == 0
    output = capsys.readouterr().out
    assert "Duplicate groups: 3" in output  # Complete summary before the refresh.
    assert "g001/copy0.bin" in output and "g002/copy0.bin" not in output  # Page after refresh.
    assert "Showing groups 2-2 of 3 duplicate groups." in output
    with Catalog.open(database) as catalog:
        assert catalog.duplicates(tmp_path).summary().duplicate_content_group_count == 2
