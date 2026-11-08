# Plan 002 — Catalog Refresh and Reconciliation

**Status:** Implemented (reviewed).

**Review checkpoint:** 2026-11-08 — Reviewed at the user's request for the
catalog-to-archive handoff. Earlier dated implementation/review notes and
checklists remain historical; this checkpoint supersedes earlier pending
statuses and does not activate follow-on implementation. See the
[handoff record](README.md#catalog-review-and-archive-handoff--2026-11-08).

**Parent plan:** [Plan 001 — Catalog Foundation](001-catalog-foundation.md)  
**Sequence:** 002  
**Implemented:** 2026-08-20

## Goal

Refresh a local catalog through separate filesystem reconciliation, typed change-set inspection, and atomic application. The implementation identifies new, unchanged, modified, and missing paths without treating paths as content identity.

## Public API

- `Catalog.reconcile_directory(root, ...) -> RefreshChangeSet` performs read-only filesystem observation and writes nothing to SQLite.
- `Catalog.apply_refresh(change_set) -> ScanSummary` verifies the catalog baseline and persists a completed snapshot atomically.
- `Catalog.scan_directory()` remains the compatibility wrapper that reconciles then applies.
- `RefreshChangeSet` carries a location, an optional baseline current-scan ID, ordered `RefreshChange` values, and aggregate `RefreshSummary` counts.
- `RefreshChange` kinds are `new`, `unchanged`, `modified`, and `missing`; it exposes prior/current observations and whether the content ID was reused from matching metadata.

## Design decisions

- A matching relative path, size, and `mtime_ns` reuses the existing SHA-256 identity without re-hashing. This is a pragmatic cache, not cryptographic proof that bytes are unchanged.
- Metadata-only changes are re-hashed; if the content ID remains equal, they are reported as `unchanged` and persist the new metadata.
- A rename is represented as `missing` plus `new`; no rename policy or inference is introduced.
- The baseline check detects catalog staleness only: another successful refresh of the location. A change set is a filesystem observation captured during reconciliation and neither locks nor snapshots the filesystem after that point.
- Hashed files are checked through both their pathname and open descriptor before and after reading. Detectable size, mtime, type, device, or inode changes fail reconciliation, so no such observation can be applied. Deliberately restoring every checked metadata field remains outside what ordinary filesystem observation can detect.
- Traversal sorts one directory at a time, and reconciliation streams the current snapshot alongside it. The returned change set itself is materialized because it is the deliberate dry-run/comparison boundary; no additional catalog-wide path map or global traversal sort is used.
- Schema version remains `1`: historical scan rows and `locations.current_scan_id` already supply the required current-state and baseline relationship. No `previous_scan_id` is needed.

## Validation

Added coverage for write-free initial reconciliation, metadata-cache reuse, metadata-only updates, new/modified/missing changes, stale change sets, and reconciliation failure preserving current state. Existing scan and CLI tests were updated for the write-free failure boundary and CLI refresh summaries.

- `uv run pytest`: 49 passed, 1 skipped
- `uv run ruff check .`: passed
- `uv run ruff format --check .`: passed
- `uv run mypy src tests`: passed
- `git diff --check`: passed
## Follow-up — 2026-11-08

[Plan 002.2 — Refresh Hash Verification](002.2-refresh-hash-verification.md)
extends both reconciliation and the scan convenience wrapper with optional full
SHA-256 verification. Matching-metadata reuse remains the default; callers may
bypass it with `verify_hashes=True`. The original implementation outcome above
is preserved.

## Ordering correction — 2026-11-08

[Bugfix 006](../bugfixes/006-reconciliation-path-ordering.md) makes the baseline
query agree with depth-first traversal and `PurePosixPath` merge comparisons:
reconciliation uses POSIX component order, not complete path-string order.
This corrects false new/missing classifications for a directory beside a
similarly named ZIP file. Public read-query ordering and schema are unchanged.

## Explicit retention follow-up — 2026-11-08

[Plan 002.3](002.3-catalog-scan-history-pruning.md) adds per-scan summary reads
and explicit transactional pruning. Current snapshots, content, and tags remain;
retained scan IDs stay unchanged. Optional failed cleanup is limited to rows
before the retained successful window. Pruning never runs during refresh, and
previously reconciled current-baseline change sets remain applicable. Earlier
implementation outcomes are preserved.
