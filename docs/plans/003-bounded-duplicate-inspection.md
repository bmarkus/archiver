# Plan 003 — Bounded Duplicate Inspection

**Status:** Implemented (pending commit)  
**Parent plan:** [Plan 001 — Catalog Foundation](001-catalog-foundation.md)  
**Related plans:** [001.3](001.3-catalog-cli.md), [001.5](001.5-bounded-current-file-browsing.md), [002](002-catalog-refresh-and-reconciliation.md)  
**Sequence:** 003  
**Created:** 2026-08-23  
**Implemented:** 2026-08-23

## Goal

Make duplicate-content groups safely inspectable without implying keeper selection or deletion safety. Duplicate inspection remains observational and bounded even when a catalog contains large groups.

## Public API and CLI

- `Catalog.search_duplicate_groups(root, group_limit=20, member_limit=20) -> DuplicateGroupSearch` returns complete aggregate metrics and bounded `DuplicateGroupView` values.
- Groups are ordered by potential redundant bytes descending and then full content identity. Members are ordered by relative POSIX path.
- Group and member limits are applied by SQLite queries before observations are materialized. `Catalog.duplicate_groups()` remains compatible.
- `archiver catalog duplicates ROOT` retains its aggregate-only output.
- `--details` displays group metrics and paths; `--group-limit N` and `--member-limit N` bound the two output levels and default to 20.

## Safety and non-goals

- Inspection does not alter source files, catalog current state, or persistent schema.
- No member is nominated as a keeper and no deletion, quarantine, archive authority, merge policy, or conflict-resolution operation is introduced.
- A missing refresh observation remains historical/current-state reconciliation, not a deletion request.

## Validation

- Added integration coverage for complete totals with bounded groups and members, deterministic ordering, empty-file duplicates, compatibility output, empty states, and read-only safety.
- `uv run pytest`: 58 passed, 1 skipped.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run mypy src tests`: passed.
- `git diff --check`: passed.