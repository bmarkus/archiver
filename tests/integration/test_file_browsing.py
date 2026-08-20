from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from archiver import Catalog, ScanFailure
from archiver.cli import main


def _write_file(path: Path, contents: bytes, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_search_current_files_filters_sorts_limits_and_counts(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _write_file(root / "a-small.txt", b"a", 1_700_000_000_000_000_000)
    _write_file(root / "b-tie.txt", b"bb", 1_700_000_001_000_000_000)
    _write_file(root / "c-tie.txt", b"cc", 1_700_000_001_000_000_000)
    _write_file(root / "nested" / "recent.txt", b"ddd", 1_700_000_002_000_000_000)

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)

        default_result = catalog.search_current_files(root, limit=2)
        size_result = catalog.search_current_files(root, sort_by="size", limit=4)
        smallest_result = catalog.search_current_files(root, sort_by="size", reverse=True, limit=4)
        newest_result = catalog.search_current_files(root, sort_by="date", limit=4)
        oldest_result = catalog.search_current_files(root, sort_by="date", reverse=True, limit=4)
        nested_result = catalog.search_current_files(root, path_glob="nested/*.txt", limit=20)
        class_result = catalog.search_current_files(root, path_glob="[ab]-*.txt", limit=20)

    assert [item.relative_path.as_posix() for item in default_result.files] == ["a-small.txt", "b-tie.txt"]
    assert default_result.total_matches == 4
    assert default_result.total_size_bytes == 8
    assert [item.relative_path.as_posix() for item in size_result.files] == [
        "nested/recent.txt",
        "b-tie.txt",
        "c-tie.txt",
        "a-small.txt",
    ]
    assert [item.relative_path.as_posix() for item in smallest_result.files] == [
        "a-small.txt",
        "b-tie.txt",
        "c-tie.txt",
        "nested/recent.txt",
    ]
    assert [item.relative_path.as_posix() for item in newest_result.files] == [
        "nested/recent.txt",
        "b-tie.txt",
        "c-tie.txt",
        "a-small.txt",
    ]
    assert [item.relative_path.as_posix() for item in oldest_result.files] == [
        "a-small.txt",
        "b-tie.txt",
        "c-tie.txt",
        "nested/recent.txt",
    ]
    assert [item.relative_path.as_posix() for item in nested_result.files] == ["nested/recent.txt"]
    assert [item.relative_path.as_posix() for item in class_result.files] == ["a-small.txt", "b-tie.txt"]


def test_search_current_files_excludes_failed_scan_state(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    original = root / "original.txt"
    _write_file(original, b"first", 1_700_000_000_000_000_000)

    with Catalog.create(tmp_path / "catalog.sqlite") as catalog:
        catalog.scan_directory(root)
        original.write_bytes(b"second")

        def fail_progress(_: object) -> None:
            raise RuntimeError("controlled callback failure")

        with pytest.raises(ScanFailure, match="scan failed"):
            catalog.scan_directory(root, progress_callback=fail_progress)

        result = catalog.search_current_files(root, limit=20)

    assert [item.relative_path.as_posix() for item in result.files] == ["original.txt"]
    assert result.files[0].size_bytes == len(b"first")


def test_catalog_files_cli_renders_bounded_sorted_filtered_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    _write_file(root / "a.txt", b"a", 1_700_000_000_000_000_000)
    _write_file(root / "nested" / "b.txt", b"bbb", 1_700_000_001_000_000_000)
    _write_file(root / "c.bin", b"cc", 1_700_000_002_000_000_000)

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "scan", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "files", str(root), "--limit", "2", "--sort", "size"]) == 0

    output = capsys.readouterr().out
    assert "Sort: size (descending)" in output
    assert output.index("nested/b.txt") < output.index("c.bin")
    assert "Size" in output
    assert "Matched: 3 files · 6 B total (showing first 2)" in output

    assert main(["catalog", "files", str(root), "--path", "nested/*.txt", "--reverse"]) == 0

    filtered_output = capsys.readouterr().out
    assert "Sort: path (descending)" in filtered_output
    assert "nested/b.txt" in filtered_output
    assert "a.txt" not in filtered_output
    assert "Matched: 1 files · 3 B total (showing all 1)" in filtered_output


def test_catalog_files_cli_reports_empty_current_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "photos"
    root.mkdir()

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "files", str(root)]) == 0

    output = capsys.readouterr().out
    assert "No matching files." in output
    assert "Matched: 0 files · 0 B total (showing all 0)" in output


def test_catalog_files_cli_uses_compact_units_short_digests_and_truncated_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    filename = "a-very-long-file-name-that-needs-terminal-truncation.jpg"
    _write_file(root / filename, b"x" * 1_500_000, 1_700_000_000_000_000_000)

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "scan", str(root)]) == 0
    capsys.readouterr()
    monkeypatch.setattr("archiver.cli.shutil.get_terminal_size", lambda fallback=(80, 24): os.terminal_size((70, 24)))

    assert main(["catalog", "files", str(root)]) == 0

    output = capsys.readouterr().out
    digest = hashlib.sha256(b"x" * 1_500_000).hexdigest()
    assert "1.5 MB" in output
    assert f"{digest[:12]}…" in output
    assert digest not in output
    assert filename not in output
    assert "a-very-long-file-na…" in output
    assert "Matched: 1 files · 1.5 MB total (showing all 1)" in output
