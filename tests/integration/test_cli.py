from __future__ import annotations

import io
import tomllib
from pathlib import Path, PurePosixPath

import pytest

from archiver import Catalog, ContentId, ScanProgress
from archiver.cli import _format_byte_count, _ProgressRenderer, main
from archiver.hashing import hash_file


def test_project_registers_archiver_console_command() -> None:
    project_path = Path(__file__).parents[2] / "pyproject.toml"
    with project_path.open("rb") as project_file:
        project = tomllib.load(project_file)

    assert project["project"]["scripts"]["archiver"] == "archiver.cli:main"


def test_catalog_init_creates_database_without_scanning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "entry.txt"
    source.write_bytes(b"source content")

    assert main(["catalog", "init", str(root)]) == 0

    output = capsys.readouterr().out
    database_path = root / ".archiver" / "catalog.sqlite"
    assert database_path.is_file()
    assert "Catalog initialized" in output
    assert f"Root: {root.resolve()}" in output
    assert source.read_bytes() == b"source content"
    with Catalog.open(database_path) as catalog:
        assert catalog.current_scan(root) is None


def test_catalog_init_refuses_existing_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "photos"
    root.mkdir()

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()

    assert main(["catalog", "init", str(root)]) == 1

    assert "catalog database already exists" in capsys.readouterr().err


def test_catalog_info_reports_unscanned_and_scanned_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    (root / "entry.txt").write_bytes(b"entry")

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "info", str(root)]) == 0
    assert "Current scan: none" in capsys.readouterr().out

    assert main(["catalog", "scan", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "info", str(root)]) == 0

    output = capsys.readouterr().out
    assert "Current scan: 1" in output
    assert "Files observed: 1" in output
    assert "Total size observed: 5 B" in output
    assert "Completed at: " in output


def test_catalog_scan_excludes_control_directory_and_preserves_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "nested" / "entry.txt"
    source.parent.mkdir()
    source.write_bytes(b"source content")

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    (root / ".archiver" / "internal.txt").write_text("not source", encoding="utf-8")

    assert main(["catalog", "scan", str(root)]) == 0

    output = capsys.readouterr().out
    assert "Scan completed" in output
    assert "Files observed: 1" in output
    assert "New files: 1" in output
    assert "Unchanged files: 0" in output
    assert "Total size observed: 14 B" in output
    assert source.read_bytes() == b"source content"
    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        assert [item.relative_path.as_posix() for item in catalog.current_files(root)] == ["nested/entry.txt"]


def test_catalog_duplicates_reports_aggregate_metrics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    for filename in ("one.bin", "two.bin", "three.bin"):
        (root / filename).write_bytes(b"a")
    for filename in ("four.bin", "five.bin"):
        (root / filename).write_bytes(b"xyz")

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "scan", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "duplicates", str(root)]) == 0

    output = capsys.readouterr().out
    assert "Duplicate groups: 2" in output
    assert "Duplicate file instances: 5" in output
    assert "Potential redundant bytes: 5" in output


def test_catalog_commands_report_missing_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "photos"
    root.mkdir()

    assert main(["catalog", "info", str(root)]) == 1

    error = capsys.readouterr().err
    assert "catalog database does not exist" in error
    assert str(root / ".archiver" / "catalog.sqlite") in error


def test_failed_cli_scan_preserves_current_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "entry.txt"
    source.write_bytes(b"first")
    expected_content = hash_file(source)

    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    assert main(["catalog", "scan", str(root)]) == 0
    capsys.readouterr()
    source.write_bytes(b"second")

    def fail_hashing(path: Path) -> ContentId:
        raise OSError("controlled hashing failure")

    monkeypatch.setattr("archiver.catalog.hash_file_stably", fail_hashing)
    assert main(["catalog", "scan", str(root)]) == 1

    assert "reconciliation failed" in capsys.readouterr().err
    with Catalog.open(root / ".archiver" / "catalog.sqlite") as catalog:
        assert catalog.current_files(root)[0].content_id == expected_content


def test_invalid_cli_syntax_uses_argparse_exit_code() -> None:
    with pytest.raises(SystemExit) as error:
        main(["catalog", "unknown"])

    assert error.value.code == 2


class _TerminalBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_catalog_scan_renders_progress_only_to_interactive_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    (root / "entry.txt").write_bytes(b"entry")
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    terminal = _TerminalBuffer()
    monkeypatch.setattr("archiver.cli.sys.stderr", terminal)

    assert main(["catalog", "scan", str(root)]) == 0

    assert "Scanned 1 files, 5 B" in terminal.getvalue()
    assert terminal.getvalue().endswith("\n")
    assert "Scan completed" in capsys.readouterr().out


def test_catalog_scan_no_progress_and_noninteractive_output_are_stable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    (root / "entry.txt").write_bytes(b"entry")
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    terminal = _TerminalBuffer()
    monkeypatch.setattr("archiver.cli.sys.stderr", terminal)

    assert main(["catalog", "scan", str(root), "--no-progress"]) == 0

    assert terminal.getvalue() == ""
    assert "Scan completed" in capsys.readouterr().out

    noninteractive = io.StringIO()
    monkeypatch.setattr("archiver.cli.sys.stderr", noninteractive)
    assert main(["catalog", "scan", str(root)]) == 0

    assert noninteractive.getvalue() == ""


def test_catalog_scan_progress_ends_before_error_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    (root / "first.txt").write_bytes(b"first")
    (root / "second.txt").write_bytes(b"second")
    assert main(["catalog", "init", str(root)]) == 0
    capsys.readouterr()
    terminal = _TerminalBuffer()
    monkeypatch.setattr("archiver.cli.sys.stderr", terminal)

    def fail_hashing(path: Path) -> tuple[ContentId, int, int]:
        if path.name == "second.txt":
            raise OSError("controlled hashing failure")
        content_id = hash_file(path)
        metadata = path.stat()
        return content_id, metadata.st_size, metadata.st_mtime_ns

    monkeypatch.setattr("archiver.catalog.hash_file_stably", fail_hashing)
    assert main(["catalog", "scan", str(root)]) == 1

    assert "Scanned 1 files" in terminal.getvalue()
    assert "\narchiver: reconciliation failed" in terminal.getvalue()


def test_progress_renderer_clears_a_shorter_filename_and_honors_file_interval() -> None:
    terminal = _TerminalBuffer()
    renderer = _ProgressRenderer(terminal, every_files=2)
    long_path = "a-very-long-file-name-that-should-not-leave-a-tail.txt"
    first = ScanProgress(1, 10, 0.1, PurePosixPath(long_path))
    second = ScanProgress(2, 20, 0.2, PurePosixPath("short.txt"))
    skipped = ScanProgress(3, 30, 0.3, PurePosixPath("ignored.txt"))

    renderer.render(first)
    renderer.render(second)
    renderer.render(skipped)
    renderer.finish()

    first_line = "Scanned 1 files, 10 B, 0.1s: " + long_path
    second_line = "Scanned 2 files, 20 B, 0.2s: short.txt"
    padding = " " * (len(first_line) - len(second_line))
    assert terminal.getvalue() == f"\r{first_line}\r{first_line}\r{second_line}{padding}\r{second_line}\n"


def test_progress_byte_count_uses_compact_decimal_units() -> None:
    assert _format_byte_count(0) == "0 B"
    assert _format_byte_count(999) == "999 B"
    assert _format_byte_count(1_000) == "1.0 KB"
    assert _format_byte_count(1_500_000) == "1.5 MB"
    assert _format_byte_count(2_000_000_000) == "2.0 GB"
