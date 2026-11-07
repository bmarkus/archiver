"""The CLI group table can avoid member queries entirely."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from archiver import Catalog, DuplicateGroupQuery
from archiver.cli import main


def test_duplicate_details_default_to_bounded_metrics_without_member_queries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(25):
        directory = tmp_path / "nested" / f"g{index:03}"
        directory.mkdir(parents=True)
        for member in range(2):
            (directory / f"copy{member}.bin").write_bytes(bytes([index]) * index)
    assert main(["catalog", "init", str(tmp_path)]) == 0
    assert main(["catalog", "refresh", str(tmp_path), "--no-progress"]) == 0
    capsys.readouterr()
    database = tmp_path / ".archiver" / "catalog.sqlite"
    before_database = database.read_bytes()
    before_source = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.rglob("*.bin")}

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("metrics-only tables must not fetch member previews")

    with monkeypatch.context() as patch:
        patch.setattr("archiver._sqlite_query.compile_duplicate_group_members", forbidden)
        patch.setattr(DuplicateGroupQuery, "fetch", forbidden)
        args = ["catalog", "duplicates", str(tmp_path), "--details"]
        assert main(args) == 0
        default = capsys.readouterr()
        assert default.err == ""
        assert "GROUP  SHA-256        COPIES  SIZE   POTENTIAL REDUNDANT\n" in default.out
        assert "PATHS" not in default.out and ".bin" not in default.out
        assert "Duplicate groups: 25" in default.out and "Duplicate file instances: 50" in default.out
        assert "Showing first 20 of 25 duplicate groups." in default.out
        assert len(re.findall(r"^\d+\s+[0-9a-f]{12}…", default.out, re.MULTILINE)) == 20
        assert main([*args, "--member-limit", "0"]) == 0
        assert capsys.readouterr() == default
        assert main([*args, "--group-offset", "20", "--group-limit", "3"]) == 0
        page = capsys.readouterr().out
        digest = hashlib.sha256(bytes([4]) * 4).hexdigest()[:12]
        assert f"21     {digest}" in page
        assert "Showing groups 21-23 of 25 duplicate groups." in page
        assert ".bin" not in page and "PATHS" not in page
        assert main([*args, "--group-limit", "25"]) == 0
        all_groups = capsys.readouterr().out
        empty_digest = hashlib.sha256(b"").hexdigest()[:12]
        assert f"25     {empty_digest}" in all_groups and "0 B" in all_groups

    assert main([*args, "--group-offset", "20", "--group-limit", "3", "--member-limit", "1"]) == 0
    preview = capsys.readouterr().out
    assert "PATHS" in preview and "1 of 2" in preview
    assert "\n\t       nested/g004/copy0.bin\n" in preview
    assert "copy1.bin" not in preview
    assert "Showing groups 21-23 of 25 duplicate groups." in preview
    assert database.read_bytes() == before_database
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.rglob("*.bin")
    } == before_source


@pytest.mark.parametrize("options", [["--member-limit", "-1"], ["--member-limit", "invalid"], ["--group-limit", "0"]])
def test_invalid_duplicate_table_options_fail_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Catalog:
        pytest.fail("invalid display bounds must be rejected before catalog open")

    monkeypatch.setattr(Catalog, "open", forbidden)
    with pytest.raises(SystemExit) as raised:
        main(["catalog", "duplicates", str(tmp_path), "--details", *options])
    assert raised.value.code == 2
    assert not (tmp_path / ".archiver").exists()


def test_duplicate_help_describes_metrics_only_default(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["catalog", "duplicates", "--help"])
    assert raised.value.code == 0
    assert "0 shows only group metrics (default: 0)" in " ".join(capsys.readouterr().out.split())
