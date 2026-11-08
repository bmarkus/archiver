from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from archiver import Catalog, HistoryPruningError, ScanHistorySummary
from archiver.cli import main


def _setup(root: Path) -> Path:
    (root / ".archiver").mkdir(parents=True)
    database = root / ".archiver/catalog.sqlite"
    with Catalog.create(database) as catalog:
        # No physical source access is needed after these empty successful scans.
        for _ in range(5):
            catalog.scan_directory(root, excluded_directories=(root / ".archiver",))
        with catalog._connection:
            catalog._connection.execute("UPDATE scan_runs SET started_at_ns=0, completed_at_ns=0")
            catalog._connection.execute("INSERT INTO scan_runs VALUES(0,1,0,0,'failed')")
            catalog._connection.execute("INSERT INTO scan_runs VALUES(6,1,0,NULL,'running')")
            catalog._connection.execute("INSERT INTO scan_runs VALUES(7,1,0,0,'failed')")
    return database


def _dump(database: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.iterdump())


def test_history_lists_dates_partial_empty_totals_and_current_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    database = _setup(root)
    before = _dump(database)
    assert main(["catalog", "history", str(root)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "UTC" in output.out and "1970-01-01T00:00:00.000000Z" in output.out
    assert "5  completed  *" in output.out
    assert "0 B  partial" in output.out and "0 B  complete" in output.out
    lines = [line for line in output.out.splitlines() if line.split() and line.split()[0].isdigit()]
    assert [int(line.split()[0]) for line in lines] == [7, 6, 5, 4, 3, 2, 1, 0]
    assert "  -  0  0 B  partial" in lines[1]  # no completion time for running scan
    assert main(["catalog", "history", str(root), "--limit", "2"]) == 0
    limited = capsys.readouterr().out
    assert "7  failed" in limited and "6  running" in limited and "5  completed" not in limited
    assert _dump(database) == before


@pytest.mark.parametrize("include_failed", [False, True])
def test_cli_pruning_defaults_to_preview_and_apply_matches_api(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], include_failed: bool
) -> None:
    root = tmp_path / "source"
    database = _setup(root)
    before = _dump(database)
    args = ["catalog", "prune-history", str(root)]
    if include_failed:
        args.append("--include-failed")
    with Catalog.open(database) as catalog:
        expected = catalog.prune_history(root, include_failed=include_failed)
    assert main(args) == 0
    output = capsys.readouterr()
    assert "History pruning preview" in output.out
    assert "No catalog changes were written" in output.out
    assert f"Scans to remove: {expected.scans_to_remove}" in output.out
    assert "Successful scans retained: 3" in output.out
    assert _dump(database) == before
    if include_failed:
        assert "scan IDs before 3 only" in output.out
    assert main([*args, "--apply"]) == 0
    applied = capsys.readouterr()
    assert "History pruning completed" in applied.out
    assert f"Scans removed: {expected.scans_to_remove}" in applied.out
    with Catalog.open(database) as catalog:
        assert [row.scan.id for row in catalog.scan_summaries(root)] == (
            [7, 6, 5, 4, 3] if include_failed else [7, 6, 5, 4, 3, 0]
        )
    assert main([*args, "--keep-last", "1", "--apply"]) == 0
    assert "Successful scans retained: 1" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["history", "prune-history"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "no", str(2**63)])
def test_cli_rejects_invalid_bounds_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, value: str
) -> None:
    def no_open(*args: object) -> None:
        raise AssertionError("invalid arguments must not open a catalog")

    monkeypatch.setattr(Catalog, "open", no_open)
    with pytest.raises(SystemExit) as failure:
        main(["catalog", command, str(tmp_path), "--limit" if command == "history" else "--keep-last", value])
    assert failure.value.code == 2


@pytest.mark.parametrize("command", ["history", "prune-history"])
def test_cli_help_missing_catalog_and_empty_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["catalog", command, "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert ("--limit" if command == "history" else "--include-failed") in help_text
    assert main(["catalog", command, str(tmp_path)]) == 1
    assert "catalog" in capsys.readouterr().err
    control = tmp_path / ".archiver"
    control.mkdir()
    with Catalog.create(control / "catalog.sqlite"):
        pass
    before = _dump(control / "catalog.sqlite")
    assert main(["catalog", command, str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert ("No scan history." if command == "history" else "Scans to remove: 0") in output
    if command == "prune-history":
        assert main(["catalog", command, str(tmp_path), "--include-failed"]) == 0
        assert "No successful-scan cutoff" in capsys.readouterr().out
    assert _dump(control / "catalog.sqlite") == before


def test_cli_history_closes_iterator_at_limit_and_on_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    database = _setup(root)
    with Catalog.open(database) as catalog:
        values = tuple(catalog.scan_summaries(root))
    closed: list[bool] = []

    def tracked(self: Catalog, root: Path) -> Generator[ScanHistorySummary, None, None]:
        try:
            yield from values
        finally:
            closed.append(True)

    monkeypatch.setattr(Catalog, "scan_summaries", tracked)
    assert main(["catalog", "history", str(root), "--limit", "1"]) == 0
    assert closed == [True]

    def failed(self: Catalog, root: Path) -> Generator[ScanHistorySummary, None, None]:
        try:
            yield values[0]
            raise sqlite3.OperationalError("injected inspection failure")
        finally:
            closed.append(True)

    monkeypatch.setattr(Catalog, "scan_summaries", failed)
    assert main(["catalog", "history", str(root)]) == 1
    assert closed == [True, True]
    assert "could not read scan history" in capsys.readouterr().err


def test_cli_pruning_errors_do_not_print_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    database = _setup(root)
    before = _dump(database)

    def fail(self: Catalog, *args: Any, **kwargs: Any) -> None:
        raise HistoryPruningError("injected pruning failure")

    monkeypatch.setattr(Catalog, "prune_history", fail)
    assert main(["catalog", "prune-history", str(root), "--apply"]) == 1
    output = capsys.readouterr()
    assert "completed" not in output.out
    assert "injected pruning failure" in output.err
    assert _dump(database) == before
