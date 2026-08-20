"""Command-line interface for an in-root Archiver catalog."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from .catalog import Catalog
from .errors import InvalidCatalogError, ScanFailure
from .models import DuplicateSummary, ScanProgress, ScanRun, ScanSummary

_CONTROL_DIRECTORY_NAME = ".archiver"
_DATABASE_FILE_NAME = "catalog.sqlite"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Archiver command-line interface."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    try:
        if arguments.catalog_command == "init":
            _initialize_catalog(arguments.root)
        elif arguments.catalog_command == "info":
            _show_catalog_info(arguments.root)
        elif arguments.catalog_command == "scan":
            _scan_catalog(arguments.root, no_progress=arguments.no_progress, progress_every=arguments.progress_every)
        elif arguments.catalog_command == "duplicates":
            _show_duplicate_summary(arguments.root)
        else:
            parser.error("a catalog command is required")
    except (InvalidCatalogError, ScanFailure, OSError) as error:
        print(f"archiver: {error}", file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archiver", description="Content-oriented file cataloging.")
    command_parsers = parser.add_subparsers(dest="command", required=True)
    catalog_parser = command_parsers.add_parser("catalog", help="create and inspect an in-root catalog")
    catalog_commands = catalog_parser.add_subparsers(dest="catalog_command", required=True)

    for command, help_text in (
        ("init", "create a catalog without scanning"),
        ("info", "show catalog and current-scan information"),
        ("scan", "scan the catalog root"),
        ("duplicates", "show aggregate duplicate metrics"),
    ):
        subparser = catalog_commands.add_parser(command, help=help_text)
        subparser.add_argument("root", type=Path, metavar="ROOT")
        if command == "scan":
            subparser.add_argument("--no-progress", action="store_true", help="suppress terminal scan progress")
            subparser.add_argument(
                "--progress-every",
                type=_positive_int,
                default=100,
                metavar="FILES",
                help="render progress every FILES files (default: 100)",
            )

    return parser


def _initialize_catalog(root_argument: Path) -> None:
    root = _canonical_root(root_argument)
    control_directory = root / _CONTROL_DIRECTORY_NAME
    if control_directory.is_symlink():
        raise InvalidCatalogError(f"catalog control directory must not be a symbolic link: {control_directory}")
    if control_directory.exists() and not control_directory.is_dir():
        raise InvalidCatalogError(f"catalog control path is not a directory: {control_directory}")
    control_directory.mkdir(exist_ok=True)
    database_path = control_directory / _DATABASE_FILE_NAME
    with Catalog.create(database_path) as catalog:
        print("Catalog initialized")
        print(f"Root: {root}")
        print(f"Database: {catalog.database_path}")
        print(f"Catalog UUID: {catalog.catalog_uuid}")


def _show_catalog_info(root_argument: Path) -> None:
    root, database_path = _catalog_paths(root_argument)
    with Catalog.open(database_path) as catalog:
        print("Catalog")
        print(f"Root: {root}")
        print(f"Database: {catalog.database_path}")
        print(f"Catalog UUID: {catalog.catalog_uuid}")
        print(f"Schema version: {catalog.schema_version}")
        current_scan = catalog.current_scan(root)
        current_summary = catalog.current_summary(root)
        if current_scan is None or current_summary is None:
            print("Current scan: none")
        else:
            _print_current_scan(current_scan, current_summary)


def _scan_catalog(root_argument: Path, *, no_progress: bool, progress_every: int) -> None:
    root, database_path = _catalog_paths(root_argument)
    renderer = (
        _ProgressRenderer(sys.stderr, every_files=progress_every) if sys.stderr.isatty() and not no_progress else None
    )
    with Catalog.open(database_path) as catalog:
        try:
            summary = catalog.scan_directory(
                root,
                excluded_directories=(root / _CONTROL_DIRECTORY_NAME,),
                progress_callback=None if renderer is None else renderer.render,
            )
        finally:
            if renderer is not None:
                renderer.finish()
        print("Scan completed")
        print(f"Root: {root}")
        _print_scan_summary(summary)


def _show_duplicate_summary(root_argument: Path) -> None:
    root, database_path = _catalog_paths(root_argument)
    with Catalog.open(database_path) as catalog:
        print("Duplicate content")
        print(f"Root: {root}")
        if catalog.current_scan(root) is None:
            print("Current scan: none")
            return
        _print_duplicate_summary(catalog.duplicate_summary(root))


def _catalog_paths(root_argument: Path) -> tuple[Path, Path]:
    root = _canonical_root(root_argument)
    return root, root / _CONTROL_DIRECTORY_NAME / _DATABASE_FILE_NAME


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _canonical_root(root: Path) -> Path:
    if root.is_symlink():
        raise InvalidCatalogError(f"catalog root must not be a symbolic link: {root}")
    if not root.is_dir():
        raise InvalidCatalogError(f"catalog root is not a directory: {root}")
    return root.resolve(strict=True)


def _print_current_scan(scan: ScanRun, summary: ScanSummary) -> None:
    print(f"Current scan: {scan.id}")
    assert scan.completed_at_ns is not None
    print(f"Completed at: {_format_utc(scan.completed_at_ns)}")
    _print_scan_summary(summary)


def _print_scan_summary(summary: ScanSummary) -> None:
    print(f"Files observed: {summary.files_observed}")
    print(f"Total size observed: {_format_byte_count(summary.total_bytes_observed)}")
    print(f"Distinct content: {summary.distinct_content_count}")
    print(f"Duplicate groups: {summary.duplicate_content_group_count}")


def _print_duplicate_summary(summary: DuplicateSummary) -> None:
    print(f"Duplicate groups: {summary.duplicate_content_group_count}")
    print(f"Duplicate file instances: {summary.duplicate_file_instance_count}")
    print(f"Potential redundant bytes: {summary.potential_redundant_bytes}")


def _format_byte_count(byte_count: int) -> str:
    """Format a byte count for compact terminal progress output."""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(byte_count)
    for unit in units:
        if value < 1_000 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{byte_count} B"
        value /= 1_000
    raise AssertionError("unreachable")


def _format_utc(timestamp_ns: int) -> str:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    timestamp = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanoseconds // 1_000)
    return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


class _ProgressRenderer:
    """Best-effort, throttled terminal rendering for scan progress."""

    def __init__(self, stream: TextIO, *, every_files: int) -> None:
        self._stream = stream
        self._every_files = every_files
        self._enabled = True
        self._rendered = False
        self._previous_line_length = 0

    def render(self, progress: ScanProgress) -> None:
        """Render an initial update and then every configured number of completed files."""
        if not self._enabled or (self._rendered and progress.files_observed % self._every_files != 0):
            return
        line = (
            f"Scanned {progress.files_observed} files, {_format_byte_count(progress.total_bytes_observed)}, "
            f"{progress.elapsed_seconds:.1f}s: {progress.current_relative_path.as_posix()}"
        )
        padding = " " * max(self._previous_line_length - len(line), 0)
        try:
            self._stream.write(f"\r{line}{padding}\r{line}")
            self._stream.flush()
        except OSError:
            self._enabled = False
            return
        self._rendered = True
        self._previous_line_length = len(line)

    def finish(self) -> None:
        """Terminate a live status line before command success or error output."""
        if not self._enabled or not self._rendered:
            return
        try:
            self._stream.write("\n")
            self._stream.flush()
        except OSError:
            self._enabled = False
