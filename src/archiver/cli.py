"""Command-line interface for an in-root Archiver catalog."""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from .catalog import Catalog
from .errors import InvalidCatalogError, ScanFailure
from .models import (
    CurrentFileSort,
    DuplicateGroupSearch,
    DuplicateGroupView,
    DuplicateSummary,
    FileObservation,
    RefreshChange,
    RefreshChangeSet,
    RefreshSummary,
    ScanProgress,
    ScanRun,
    ScanSummary,
)

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
        elif arguments.catalog_command in ("refresh", "scan"):
            _refresh_catalog(
                arguments.root,
                dry_run=arguments.dry_run,
                details=arguments.details,
                no_progress=arguments.no_progress,
                progress_every=arguments.progress_every,
            )
        elif arguments.catalog_command == "duplicates":
            _show_duplicate_summary(
                arguments.root,
                details=arguments.details,
                group_limit=arguments.group_limit,
                member_limit=arguments.member_limit,
            )
        elif arguments.catalog_command == "files":
            _show_files(
                arguments.root,
                path_glob=arguments.path_glob,
                limit=arguments.limit,
                sort_by=arguments.sort_by,
                reverse=arguments.reverse,
            )
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
        ("refresh", "reconcile and atomically refresh the catalog root"),
        ("scan", "compatibility alias for refresh"),
        ("duplicates", "show aggregate duplicate metrics"),
        ("files", "browse bounded current-file results"),
    ):
        subparser = catalog_commands.add_parser(command, help=help_text)
        subparser.add_argument("root", type=Path, metavar="ROOT")
        if command in ("refresh", "scan"):
            subparser.add_argument(
                "--dry-run",
                action="store_true",
                help="report reconciliation results without writing the catalog",
            )
            subparser.add_argument(
                "--details",
                choices=("changes", "all"),
                help="show changed paths, or all reconciled paths",
            )
            subparser.add_argument("--no-progress", action="store_true", help="suppress terminal scan progress")
            subparser.add_argument(
                "--progress-every",
                type=_positive_int,
                default=100,
                metavar="FILES",
                help="render progress every FILES files (default: 100)",
            )
        if command == "duplicates":
            subparser.add_argument("--details", action="store_true", help="show bounded duplicate groups and paths")
            subparser.add_argument(
                "--group-limit",
                type=_positive_int,
                default=20,
                metavar="N",
                help="maximum groups to display (default: 20)",
            )
            subparser.add_argument(
                "--member-limit",
                type=_positive_int,
                default=20,
                metavar="N",
                help="maximum paths to display per group (default: 20)",
            )
        if command == "files":
            subparser.add_argument("--path", dest="path_glob", metavar="GLOB", help="match POSIX relative paths")
            subparser.add_argument(
                "--limit",
                type=_positive_int,
                default=20,
                metavar="N",
                help="maximum rows to display (default: 20)",
            )
            subparser.add_argument(
                "--sort",
                dest="sort_by",
                choices=("path", "size", "date"),
                default="path",
                help="sort by path, size, or observed modification date (default: path)",
            )
            subparser.add_argument("--reverse", action="store_true", help="invert the selected sort direction")

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


def _refresh_catalog(
    root_argument: Path,
    *,
    dry_run: bool,
    details: str | None,
    no_progress: bool,
    progress_every: int,
) -> None:
    root, database_path = _catalog_paths(root_argument)
    renderer = (
        _ProgressRenderer(sys.stderr, every_files=progress_every) if sys.stderr.isatty() and not no_progress else None
    )
    with Catalog.open(database_path) as catalog:
        try:
            change_set = catalog.reconcile_directory(
                root,
                excluded_directories=(root / _CONTROL_DIRECTORY_NAME,),
                progress_callback=None if renderer is None else renderer.render,
            )
            summary = None if dry_run else catalog.apply_refresh(change_set)
        finally:
            if renderer is not None:
                renderer.finish()
        print("Refresh preview" if dry_run else "Refresh completed")
        print(f"Root: {root}")
        _print_refresh_summary(change_set.summary)
        if details is not None:
            _print_refresh_details(change_set, include_unchanged=details == "all")
        if dry_run:
            print("No catalog changes were written.")
        else:
            assert summary is not None
            _print_scan_summary(summary)


def _show_duplicate_summary(
    root_argument: Path,
    *,
    details: bool,
    group_limit: int,
    member_limit: int,
) -> None:
    root, database_path = _catalog_paths(root_argument)
    with Catalog.open(database_path) as catalog:
        print("Duplicate content")
        print(f"Root: {root}")
        if catalog.current_scan(root) is None:
            print("Current scan: none")
            return
        if not details:
            _print_duplicate_summary(catalog.duplicate_summary(root))
            return
        result = catalog.search_duplicate_groups(root, group_limit=group_limit, member_limit=member_limit)
        _print_duplicate_summary(result.summary)
        _print_duplicate_details(result)


def _show_files(
    root_argument: Path,
    *,
    path_glob: str | None,
    limit: int,
    sort_by: CurrentFileSort,
    reverse: bool,
) -> None:
    root, database_path = _catalog_paths(root_argument)
    with Catalog.open(database_path) as catalog:
        result = catalog.search_current_files(
            root,
            path_glob=path_glob,
            limit=limit,
            sort_by=sort_by,
            reverse=reverse,
        )
    direction = _sort_direction(sort_by, reverse)
    print("Current files")
    print(f"Root: {root}")
    print(f"Sort: {sort_by} ({direction})")
    if not result.files:
        print("No matching files.")
    else:
        for line in _format_file_table(result.files, _terminal_width()):
            print(line)
    print(_format_file_match_summary(result.total_matches, result.total_size_bytes, len(result.files)))


def _format_file_table(files: tuple[FileObservation, ...], terminal_width: int) -> tuple[str, ...]:
    modified_width = 20
    size_width = 10
    digest_width = 13
    separator_width = 9
    path_width = max(20, terminal_width - modified_width - size_width - digest_width - separator_width)
    header = f"{'Modified (UTC)':<{modified_width}} | {'Size':>{size_width}} | {'SHA-256':<{digest_width}} | Path"
    divider = "-" * len(header)
    rows = tuple(
        f"{_format_file_mtime(file_observation.mtime_ns):<{modified_width}} | "
        f"{_format_byte_count(file_observation.size_bytes):>{size_width}} | "
        f"{_short_digest(file_observation.content_id.digest):<{digest_width}} | "
        f"{_truncate_text(file_observation.relative_path.as_posix(), path_width)}"
        for file_observation in files
    )
    return (header, divider, *rows)


def _format_file_match_summary(total_matches: int, total_size_bytes: int, displayed_count: int) -> str:
    summary = f"Matched: {total_matches:,} files · {_format_byte_count(total_size_bytes)} total"
    if displayed_count < total_matches:
        return f"{summary} (showing first {displayed_count})"
    return f"{summary} (showing all {displayed_count})"


def _terminal_width() -> int:
    return shutil.get_terminal_size(fallback=(120, 24)).columns


def _format_file_mtime(timestamp_ns: int) -> str:
    seconds, _ = divmod(timestamp_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _short_digest(digest: str) -> str:
    return f"{digest[:12]}…"


def _truncate_text(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return f"{text[: width - 1]}…"


def _sort_direction(sort_by: CurrentFileSort, reverse: bool) -> str:
    direction = "ascending" if sort_by == "path" else "descending"
    if reverse:
        return "descending" if direction == "ascending" else "ascending"
    return direction


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


def _print_refresh_summary(summary: RefreshSummary) -> None:
    print(f"New files: {summary.new_files}")
    print(f"Unchanged files: {summary.unchanged_files}")
    print(f"Modified files: {summary.modified_files}")
    print(f"Missing files: {summary.missing_files}")


def _print_refresh_details(change_set: RefreshChangeSet, *, include_unchanged: bool) -> None:
    changes = tuple(change for change in change_set.changes if include_unchanged or change.kind != "unchanged")
    if not changes:
        print("No path changes.")
        return
    print("Changes")
    for change in changes:
        print(_format_refresh_change(change))


def _format_refresh_change(change: RefreshChange) -> str:
    detail = ""
    if change.kind == "unchanged":
        detail = " (hash reused)" if change.hash_reused else " (hash rechecked)"
    return f"{change.kind.upper():<9} {change.relative_path.as_posix()}{detail}"


def _print_duplicate_summary(summary: DuplicateSummary) -> None:
    print(f"Duplicate groups: {summary.duplicate_content_group_count}")
    print(f"Duplicate file instances: {summary.duplicate_file_instance_count}")
    print(f"Potential redundant bytes: {summary.potential_redundant_bytes}")


def _print_duplicate_details(result: DuplicateGroupSearch) -> None:
    if not result.groups:
        print("No duplicate groups.")
        return
    print("GROUP  SHA-256        COPIES  SIZE   POTENTIAL REDUNDANT  PATHS")
    print("-----  ------------   ------  -----  -------------------  -----")
    for index, group in enumerate(result.groups, start=1):
        _print_duplicate_group(index, group)
    displayed = len(result.groups)
    total = result.summary.duplicate_content_group_count
    qualifier = "all" if displayed == total else "first"
    print(f"Showing {qualifier} {displayed} of {total} duplicate groups.")


def _print_duplicate_group(index: int, group: DuplicateGroupView) -> None:
    displayed = len(group.members)
    path_count = f"{displayed} of {group.file_instance_count}"
    print(
        f"{index:<5}  {_short_digest(group.content_id.digest):<12}  "
        f"{group.file_instance_count:<6}  {_format_byte_count(group.size_bytes):<5}  "
        f"{_format_byte_count(group.potential_redundant_bytes):<19}  {path_count}"
    )
    for member in group.members:
        print(f"       {member.relative_path.as_posix()}")


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
