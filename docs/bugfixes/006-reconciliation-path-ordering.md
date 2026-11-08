# Bugfix 006 — Reconciliation path ordering

**Status:** Implemented (reviewed).

**Review checkpoint:** 2026-11-08 — Reviewed at the user's request for the
catalog-to-archive handoff. Earlier dated implementation/review notes and
checklists remain historical; this checkpoint supersedes earlier pending
statuses and does not activate follow-on implementation. See the
[handoff record](../plans/README.md#catalog-review-and-archive-handoff--2026-11-08).

## Reproduction

Create `bundle.zip` beside `bundle/leaf.txt`, scan, then refresh without
changing either file. Existing paths can be reported as both new and missing.
Changing one unrelated file still produces these false changes.

Read-only inspection of the reported catalog found two successful snapshots
with the same 1,146 paths and one content change. Replaying reconciliation from
those recorded observations reproduced exactly 127 new, 127 missing, 1 modified,
and 1,018 unchanged. All 127 new/missing reports referred to the same paths.
Tests must reproduce this with temporary fixtures, never the user's catalog.

## Root cause and affected invariants

The merge compares `PurePosixPath` values (component order) against a baseline
stream sorted by SQLite's complete path-string order. Depth-first traversal
sorts entries within each directory, which agrees with component order.
SQLite sorts `bundle.zip` before `bundle/leaf.txt`; component order reverses
that pair. The merge requires both input streams to use the same ordering.

Preserve invariants 1–6 (identity and read-only observations), 7–9 (successful
snapshots and history), 19–22 (failure safety and deterministic semantics).
The snapshot is complete in the observed reproduction; classification and
incremental hash reuse are wrong. No catalog repair or source mutation follows
from this bug fix.

## Proposed solution

Register a private, connection-local SQLite collation comparing POSIX path
component tuples. Use it only in `_current_observation_iterator()` so the
baseline stream, traversal, and merge comparisons agree. Document this ordering
at the three boundaries. SQLite may use a temporary sort for this query; do not
add a persistent custom-collation index, schema change, or migration. Keep
Python observation consumption streaming and traversal sorted per directory.

Public query ordering remains complete path-string order. Reconciliation and
CLI change rows retain deterministic depth-first/component order. Add a dated
clarification to the refresh plan and current API/CLI references, preserving
historical implementation outcomes.

## Non-goals

No global filesystem sort or catalog-wide Python path map; no new dependency,
API, schema, hash policy, ingest, archive, or tag behavior. Do not refresh or
modify the user's Downloads catalog. Previously staged Plan 002.2 work stays
intact; no commit or author-identity configuration is part of this request.

## Acceptance tests

- Reproduce the failure before the fix using nested directories beside ZIP,
  punctuation, shared-prefix, space, and Unicode names.
- Unchanged refresh reports no new/missing paths and one unchanged observation
  per file; incremental mode reuses every hash and verification hashes each file.
- One deterministic content change yields exactly one modified path, without
  false new/missing rows. Genuine additions/removals and file/directory
  replacements remain correctly classified, once per path.
- Cover both verification modes, create/open lifetimes, persistence/history,
  source bytes/size/mtime preservation, write-free reconciliation, and
  deterministic component ordering. Public file query ordering stays unchanged.
- Exercise both CLI aliases and dry-run/applied modes; assert exact counts,
  detailed paths, and preview persistence behavior.
- Run `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy src tests`, and `git diff --check`.
  Existing failure/interruption, stale-baseline, rollback, exclusion, and symlink
  coverage must continue passing.

## Outcome

Implemented a connection-local `archiver_path_components` collation comparing
`PurePosixPath.parts`, registered for both created and reopened catalogs. Only
the private baseline query opts into this collation. Traversal and merge
comparisons retain their existing component order. Public path queries retain
their complete-string order. No schema, persistent index, migration, dependency,
or public API change was made. Existing catalogs need no migration or repair.

Files changed for this fix: `src/archiver/catalog.py`, the traversal docstring in
`src/archiver/filesystem.py`, new
`tests/integration/test_reconciliation_ordering.py`, API/CLI references, dated
002/002.1 ordering clarifications, the plan index, and this bug-fix plan.

All 12 new regression cases failed against the prior implementation and passed
after the fix. Coverage includes prefix collisions at multiple depths, spaces,
punctuation and Unicode, both verification modes, unchanged/modified/genuine
new/missing paths, file/directory replacements, hash-call counts, deterministic
ordering, unchanged public query order, write-free observation and CLI previews,
create/reopen persistence, history retention, and source byte/size/mtime safety.
Both CLI aliases are covered with preview/application and verification on/off.
Existing failure/interruption, stale-baseline, rollback, exclusion, and symlink
tests remain part of the full suite. Every acceptance criterion was reviewed.

Validation:

- `uv run pytest`: 279 passed, 5 skipped (platform symlink availability).
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed (98 files).
- `uv run mypy src tests`: passed (46 source files).
- `git diff --check`: passed.

No unresolved questions or deviations. The user catalog was not refreshed or
modified, and previously staged Plan 002.2 changes were preserved. No subsequent
plan or commit was started.
