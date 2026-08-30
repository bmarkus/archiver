"""Command-line interface for an in-root Archiver catalog."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from textwrap import wrap
from typing import TextIO

from .catalog import SCHEMA_VERSION, Catalog
from .errors import InvalidCatalogError, ScanFailure, TaggingError
from .models import (
    AvailableTagSearch,
    AvailableTagSort,
    ContentId,
    ContentTagAssertion,
    ContentTagView,
    CurrentFileSort,
    CurrentFileTagView,
    DuplicateGroupSearch,
    DuplicateGroupView,
    DuplicateSummary,
    FileObservation,
    MultiTagContentSearch,
    RefreshChange,
    RefreshChangeSet,
    RefreshSummary,
    ScanProgress,
    ScanRun,
    ScanSummary,
    TagMatchMode,
    TagProvenance,
    TagProvenanceKind,
    TagUsage,
    validate_tag_name,
)

_CONTROL_DIRECTORY_NAME = ".archiver"
_DATABASE_FILE_NAME = "catalog.sqlite"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Archiver command-line interface."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    file_tags: tuple[str, ...] = ()
    tag_aware_files = False
    if arguments.catalog_command == "files":
        file_tags = tuple(arguments.tags or ())
        if arguments.match_any_tag and not file_tags:
            parser.error("--match-any-tag requires at least one --tag")
        if arguments.provenance is not None and not file_tags:
            parser.error("--provenance requires at least one --tag")
        tag_aware_files = bool(file_tags) or any(
            (
                arguments.show_tags,
                arguments.display_tag_limit is not None,
                arguments.all_tags,
            )
        )

    try:
        if arguments.catalog_command == "init":
            _initialize_catalog(arguments.root)
        elif arguments.catalog_command == "migrate":
            _migrate_catalog(arguments.root)
        elif arguments.catalog_command == "tags":
            _handle_tags(arguments)
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
            if tag_aware_files:
                _show_tagged_files(
                    arguments.root,
                    path_glob=arguments.path_glob,
                    tags=file_tags,
                    match="any" if arguments.match_any_tag else "all",
                    provenance=arguments.provenance,
                    limit=arguments.limit,
                    sort_by=arguments.sort_by,
                    reverse=arguments.reverse,
                    tag_limit=None if arguments.all_tags else (arguments.display_tag_limit or 3),
                    all_tags=arguments.all_tags,
                )
            else:
                _show_files(
                    arguments.root,
                    path_glob=arguments.path_glob,
                    limit=arguments.limit,
                    sort_by=arguments.sort_by,
                    reverse=arguments.reverse,
                )
        else:
            parser.error("a catalog command is required")
    except (InvalidCatalogError, ScanFailure, TaggingError, OSError) as error:
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
        ("migrate", "explicitly migrate an older catalog schema"),
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
            subparser.add_argument(
                "--tag",
                dest="tags",
                action="append",
                type=_tag_name,
                metavar="TAG",
                help="require an active content tag; repeat for AND matching",
            )
            subparser.add_argument(
                "--match-any-tag",
                action="store_true",
                help="match any requested tag instead of every requested tag",
            )
            subparser.add_argument("--provenance", choices=("user", "system"))
            subparser.add_argument("--show-tags", action="store_true", help="show active tags without filtering")
            file_tag_display = subparser.add_mutually_exclusive_group()
            file_tag_display.add_argument("--display-tag-limit", type=_positive_int, metavar="N")
            file_tag_display.add_argument(
                "--all-tags",
                action="store_true",
                help="show every active tag on bounded rows",
            )

    tags_parser = catalog_commands.add_parser("tags", help="add, remove, and query content tags")
    tag_commands = tags_parser.add_subparsers(dest="tag_command", required=True)
    for command, help_text in (
        ("add", "add a user tag to content"),
        ("remove", "remove active user tag assertions"),
        ("list", "list active assertions for content"),
    ):
        subparser = tag_commands.add_parser(command, help=help_text)
        subparser.add_argument("root", type=Path, metavar="ROOT")
        if command != "list":
            subparser.add_argument("tag", type=_tag_name, metavar="TAG")
        target = subparser.add_mutually_exclusive_group(required=True)
        target.add_argument("--path", dest="relative_path", type=_relative_path, metavar="PATH")
        target.add_argument("--content", dest="content_id", type=_content_id, metavar="SHA256")

    find_parser = tag_commands.add_parser("find", help="find bounded content identities by active tags")
    find_parser.add_argument("root", type=Path, metavar="ROOT")
    find_parser.add_argument("tags", nargs="+", type=_tag_name, metavar="TAG")
    find_parser.add_argument("--match", choices=("all", "any"), default="all")
    find_parser.add_argument("--provenance", choices=("user", "system"))
    find_parser.add_argument("--limit", type=_positive_int, default=20, metavar="N")
    find_parser.add_argument("--details", action="store_true", help="show bounded current paths")
    find_parser.add_argument("--path-limit", type=_positive_int, default=20, metavar="N")
    tag_display = find_parser.add_mutually_exclusive_group()
    tag_display.add_argument("--display-tag-limit", type=_positive_int, default=3, metavar="N")
    tag_display.add_argument("--all-tags", action="store_true", help="show every active tag on bounded rows")

    available_parser = tag_commands.add_parser("available", help="show bounded active tag usage")
    available_parser.add_argument("root", type=Path, metavar="ROOT")
    available_parser.add_argument("--regex", dest="name_regex", type=_regex_source, metavar="REGEX")
    available_parser.add_argument("--provenance", choices=("user", "system"))
    available_parser.add_argument("--limit", type=_positive_int, default=20, metavar="N")
    available_parser.add_argument(
        "--sort",
        dest="sort_by",
        choices=("name", "content", "assertions"),
        default="name",
        help="sort by name, distinct content, or active assertions (default: name)",
    )
    available_parser.add_argument("--reverse", action="store_true", help="invert the selected sort direction")

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


def _migrate_catalog(root_argument: Path) -> None:
    root, database_path = _catalog_paths(root_argument)
    changed = Catalog.migrate(database_path)
    print("Catalog migrated" if changed else "Catalog already current")
    print(f"Root: {root}")
    print(f"Schema version: {SCHEMA_VERSION}")


def _handle_tags(arguments: argparse.Namespace) -> None:
    root, database_path = _catalog_paths(arguments.root)
    if arguments.tag_command == "available":
        with Catalog.open(database_path) as catalog:
            available_result = catalog.search_available_tags(
                name_regex=arguments.name_regex,
                provenance=arguments.provenance,
                sort_by=arguments.sort_by,
                reverse=arguments.reverse,
                limit=arguments.limit,
            )
        _print_available_tags(
            root,
            name_regex=arguments.name_regex,
            provenance=arguments.provenance,
            sort_by=arguments.sort_by,
            reverse=arguments.reverse,
            result=available_result,
        )
        return
    if arguments.tag_command == "find":
        tag_limit = None if arguments.all_tags else arguments.display_tag_limit
        with Catalog.open(database_path) as catalog:
            tag_result = catalog.search_content_by_tags(
                root,
                arguments.tags,
                match=arguments.match,
                provenance=arguments.provenance,
                limit=arguments.limit,
                path_limit=arguments.path_limit,
                tag_limit=tag_limit,
            )
        _print_tag_search(
            root,
            arguments.tags,
            match=arguments.match,
            provenance=arguments.provenance,
            result=tag_result,
            details=arguments.details,
            all_tags=arguments.all_tags,
        )
        return

    provenance = _cli_tag_provenance()
    with Catalog.open(database_path) as catalog:
        if arguments.tag_command == "add":
            if arguments.content_id is not None:
                changed = catalog.add_content_tag(arguments.content_id, arguments.tag, provenance)
            else:
                changed = catalog.add_tag_for_path(root, arguments.relative_path, arguments.tag, provenance)
            print("Tag added" if changed else "Tag already active; no catalog change was needed.")
            print(f"Tag: {arguments.tag}")
            return
        if arguments.tag_command == "remove":
            if arguments.content_id is not None:
                retracted = catalog.retract_content_tag(arguments.content_id, arguments.tag)
            else:
                retracted = catalog.retract_user_tag_for_path(root, arguments.relative_path, arguments.tag)
            if retracted:
                print(f"Tag removed ({retracted} user assertion{'s' if retracted != 1 else ''} retracted)")
            else:
                print("Tag was not active; no catalog change was needed.")
            print(f"Tag: {arguments.tag}")
            return
        if arguments.tag_command == "list":
            if arguments.content_id is not None:
                assertions = catalog.tags_for_content(arguments.content_id)
                content_id = arguments.content_id
            else:
                assertions = catalog.tags_for_path(root, arguments.relative_path)
                content_id = (
                    assertions[0].content_id if assertions else catalog.content_for_path(root, arguments.relative_path)
                )
            _print_tag_assertions(content_id, assertions)
            return
    raise AssertionError(f"unsupported tag command: {arguments.tag_command}")


def _cli_tag_provenance() -> TagProvenance:
    try:
        source_version = version("archiver")
    except PackageNotFoundError:
        source_version = "0.1.0"
    return TagProvenance(kind="user", source_name="archiver-cli", source_version=source_version)


def _print_tag_assertions(content_id: ContentId, assertions: tuple[ContentTagAssertion, ...]) -> None:
    print("Content tags")
    print(f"SHA-256: {content_id.digest}")
    if not assertions:
        print("No active tags.")
        return
    for assertion in assertions:
        provenance = assertion.provenance
        detail = f" [{provenance.source_detail}]" if provenance.source_detail else ""
        print(f"{assertion.tag}  {provenance.kind}  {provenance.source_name}@{provenance.source_version}{detail}")


def _print_available_tags(
    root: Path,
    *,
    name_regex: str | None,
    provenance: TagProvenanceKind | None,
    sort_by: AvailableTagSort,
    reverse: bool,
    result: AvailableTagSearch,
) -> None:
    print("Available tags")
    print(f"Root: {root}")
    print(f"Sort: {sort_by} ({_available_tag_sort_direction(sort_by, reverse)})")
    print(f"Provenance: {provenance or 'all'}")
    if name_regex is not None:
        print(f"Regex: {name_regex}")
    if not result.tags:
        print("No matching active tags.")
    else:
        for line in _format_available_tag_table(result.tags, terminal_width=_terminal_width()):
            print(line)
    print(_format_available_tag_summary(result))


def _format_available_tag_table(tags: tuple[TagUsage, ...], *, terminal_width: int) -> tuple[str, ...]:
    content_width = max(len("Content"), *(len(f"{usage.content_count:,}") for usage in tags))
    assertions_width = max(len("Assertions"), *(len(f"{usage.assertion_count:,}") for usage in tags))
    user_width = max(len("User"), *(len(f"{usage.user_assertion_count:,}") for usage in tags))
    system_width = max(len("System"), *(len(f"{usage.system_assertion_count:,}") for usage in tags))
    separator_width = 12
    tag_width = max(
        len("Tag"),
        terminal_width - content_width - assertions_width - user_width - system_width - separator_width,
    )
    header = (
        f"{'Tag':<{tag_width}} | {'Content':>{content_width}} | "
        f"{'Assertions':>{assertions_width}} | {'User':>{user_width}} | {'System':>{system_width}}"
    )
    rows = tuple(
        f"{_truncate_text(usage.tag, tag_width):<{tag_width}} | "
        f"{usage.content_count:>{content_width},} | "
        f"{usage.assertion_count:>{assertions_width},} | "
        f"{usage.user_assertion_count:>{user_width},} | "
        f"{usage.system_assertion_count:>{system_width},}"
        for usage in tags
    )
    return (header, "-" * len(header), *rows)


def _format_available_tag_summary(result: AvailableTagSearch) -> str:
    tag_label = "active tag" if result.total_matches == 1 else "active tags"
    displayed = len(result.tags)
    qualifier = "first" if displayed < result.total_matches else "all"
    return f"Matched: {result.total_matches:,} {tag_label} (showing {qualifier} {displayed:,})"


def _print_tag_search(
    root: Path,
    requested_tags: Sequence[str],
    *,
    match: TagMatchMode,
    provenance: str | None,
    result: MultiTagContentSearch,
    details: bool,
    all_tags: bool,
) -> None:
    print("Tagged content")
    print(f"Root: {root}")
    print(f"Provenance: {provenance or 'all'}")
    if not result.contents:
        print("No matching content.")
    else:
        for line in _format_tag_content_table(
            result.contents,
            terminal_width=_terminal_width(),
            details=details,
            all_tags=all_tags,
        ):
            print(line)
    print(_format_tag_match_summary(result))
    unique_tags = tuple(dict.fromkeys(requested_tags))
    operator = " AND " if match == "all" else " OR "
    print(f"Required tags: {operator.join(unique_tags)}")


def _format_tag_content_table(
    contents: tuple[ContentTagView, ...],
    *,
    terminal_width: int,
    details: bool,
    all_tags: bool,
) -> tuple[str, ...]:
    digest_width = 13
    size_width = 10
    paths_width = max(5, max(len(f"{content.current_path_count:,}") for content in contents))
    separator_width = 9
    tag_width = max(20, terminal_width - digest_width - size_width - paths_width - separator_width)
    header = f"{'SHA-256':<{digest_width}} | {'Size':>{size_width}} | {'Paths':>{paths_width}} | Tags"
    lines = [header, "-" * len(header)]
    continuation_prefix = f"{'':<{digest_width}} | {'':>{size_width}} | {'':>{paths_width}} | "
    for content in contents:
        tag_lines = _format_content_tag_lines(content, tag_width=tag_width, all_tags=all_tags)
        lines.append(
            f"{_short_digest(content.content_id.digest):<{digest_width}} | "
            f"{_format_byte_count(content.size_bytes):>{size_width}} | "
            f"{content.current_path_count:>{paths_width},} | {tag_lines[0]}"
        )
        lines.extend(f"{continuation_prefix}{line}" for line in tag_lines[1:])
        if details:
            path_width = max(20, terminal_width - 2)
            lines.extend(f"  {_truncate_text(path.as_posix(), path_width)}" for path in content.current_paths)
    return tuple(lines)


def _format_content_tag_lines(content: ContentTagView, *, tag_width: int, all_tags: bool) -> tuple[str, ...]:
    joined_tags = ", ".join(content.tags)
    if all_tags:
        return tuple(wrap(joined_tags, width=tag_width, break_long_words=True, break_on_hyphens=False)) or ("",)
    overflow = content.active_tag_count - len(content.tags)
    suffix = f" +{overflow}" if overflow else ""
    if len(joined_tags) + len(suffix) <= tag_width:
        return (f"{joined_tags}{suffix}",)
    if suffix and tag_width > len(suffix) + 1:
        return (f"{_truncate_text(joined_tags, tag_width - len(suffix))}{suffix}",)
    return (_truncate_text(f"{joined_tags}{suffix}", tag_width),)


def _format_tag_match_summary(result: MultiTagContentSearch) -> str:
    content_label = "content identity" if result.total_matches == 1 else "content identities"
    path_label = "current path" if result.total_current_paths == 1 else "current paths"
    displayed = len(result.contents)
    qualifier = "first" if displayed < result.total_matches else "all"
    return (
        f"Matched: {result.total_matches:,} {content_label} · "
        f"{result.total_current_paths:,} {path_label} (showing {qualifier} {displayed:,})"
    )


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


def _show_tagged_files(
    root_argument: Path,
    *,
    path_glob: str | None,
    tags: tuple[str, ...],
    match: TagMatchMode,
    provenance: TagProvenanceKind | None,
    limit: int,
    sort_by: CurrentFileSort,
    reverse: bool,
    tag_limit: int | None,
    all_tags: bool,
) -> None:
    root, database_path = _catalog_paths(root_argument)
    with Catalog.open(database_path) as catalog:
        result = catalog.search_current_files_with_tags(
            root,
            path_glob=path_glob,
            tags=tags,
            match=match,
            provenance=provenance,
            limit=limit,
            sort_by=sort_by,
            reverse=reverse,
            tag_limit=tag_limit,
        )
    direction = _sort_direction(sort_by, reverse)
    print("Current files")
    print(f"Root: {root}")
    print(f"Sort: {sort_by} ({direction})")
    if not result.files:
        print("No matching files.")
    else:
        for line in _format_tagged_file_table(
            result.files,
            terminal_width=_terminal_width(),
            all_tags=all_tags,
        ):
            print(line)
    print(
        _format_tagged_file_match_summary(
            result.total_file_count,
            result.total_content_count,
            result.total_file_size_bytes,
            len(result.files),
        )
    )
    if tags:
        unique_tags = tuple(dict.fromkeys(tags))
        operator = " OR " if match == "any" else " AND "
        print(f"Required tags: {operator.join(unique_tags)}")
        print(f"Provenance: {provenance or 'all'}")


def _format_tagged_file_table(
    files: tuple[CurrentFileTagView, ...],
    *,
    terminal_width: int,
    all_tags: bool,
) -> tuple[str, ...]:
    modified_width = 20
    size_width = 10
    digest_width = 13
    separator_width = 12
    preview_width = max(
        (
            len(", ".join(file.tags))
            + len(f" +{file.active_tag_count - len(file.tags)}" if file.active_tag_count > len(file.tags) else "")
            for file in files
        ),
        default=0,
    )
    tag_width = max(20, min(40, preview_width))
    path_width = max(
        20,
        terminal_width - modified_width - size_width - digest_width - tag_width - separator_width,
    )
    header = (
        f"{'Modified (UTC)':<{modified_width}} | {'Size':>{size_width}} | "
        f"{'SHA-256':<{digest_width}} | {'Path':<{path_width}} | Tags"
    )
    lines = [header, "-" * len(header)]
    continuation_prefix = f"{'':<{modified_width}} | {'':>{size_width}} | {'':<{digest_width}} | {'':<{path_width}} | "
    for file in files:
        observation = file.observation
        tag_lines = _format_current_file_tag_lines(file, tag_width=tag_width, all_tags=all_tags)
        lines.append(
            f"{_format_file_mtime(observation.mtime_ns):<{modified_width}} | "
            f"{_format_byte_count(observation.size_bytes):>{size_width}} | "
            f"{_short_digest(observation.content_id.digest):<{digest_width}} | "
            f"{_truncate_text(observation.relative_path.as_posix(), path_width):<{path_width}} | "
            f"{tag_lines[0]}"
        )
        lines.extend(f"{continuation_prefix}{line}" for line in tag_lines[1:])
    return tuple(lines)


def _format_current_file_tag_lines(
    file: CurrentFileTagView,
    *,
    tag_width: int,
    all_tags: bool,
) -> tuple[str, ...]:
    joined_tags = ", ".join(file.tags)
    if all_tags:
        return tuple(wrap(joined_tags, width=tag_width, break_long_words=True, break_on_hyphens=False)) or ("",)
    overflow = file.active_tag_count - len(file.tags)
    suffix = f" +{overflow}" if overflow else ""
    if len(joined_tags) + len(suffix) <= tag_width:
        return (f"{joined_tags}{suffix}",)
    if suffix and tag_width > len(suffix) + 1:
        return (f"{_truncate_text(joined_tags, tag_width - len(suffix))}{suffix}",)
    return (_truncate_text(f"{joined_tags}{suffix}", tag_width),)


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


def _format_tagged_file_match_summary(
    total_file_count: int,
    total_content_count: int,
    total_file_size_bytes: int,
    displayed_count: int,
) -> str:
    file_label = "file" if total_file_count == 1 else "files"
    content_label = "distinct content" if total_content_count == 1 else "distinct contents"
    summary = (
        f"Matched: {total_file_count:,} {file_label} · "
        f"{total_content_count:,} {content_label} · "
        f"{_format_byte_count(total_file_size_bytes)} across file paths"
    )
    qualifier = "first" if displayed_count < total_file_count else "all"
    return f"{summary} (showing {qualifier} {displayed_count:,})"


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


def _available_tag_sort_direction(sort_by: AvailableTagSort, reverse: bool) -> str:
    direction = "ascending" if sort_by == "name" else "descending"
    if reverse:
        return "descending" if direction == "ascending" else "ascending"
    return direction


def _catalog_paths(root_argument: Path) -> tuple[Path, Path]:
    root = _canonical_root(root_argument)
    return root, root / _CONTROL_DIRECTORY_NAME / _DATABASE_FILE_NAME


def _tag_name(value: str) -> str:
    try:
        return validate_tag_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _regex_source(value: str) -> str:
    try:
        re.compile(value)
    except re.error as error:
        raise argparse.ArgumentTypeError(f"invalid regular expression: {error}") from error
    return value


def _content_id(value: str) -> ContentId:
    try:
        return ContentId(algorithm="sha256", digest=value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() in ("", ".") or ".." in path.parts or "\\" in value:
        raise argparse.ArgumentTypeError("must be a non-empty POSIX relative path without parent traversal")
    return path


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
