# Implementation Plans

Plans record the evolution of Archiver's design and implementation.

**Active plan:** None. Plan 005.2 remains inactive.

**Implemented (2026-11-12):** [005.1 — Targeted Catalog Updates and Inventory Retention](005.1-targeted-catalog-updates.md)
adds targeted registration/replacement/removal, snapshot-copying publication,
schema 3 origin tracking, and atomic retention defaulting to two successes for
targeted updates. Domain/invariant updates and acceptance review are complete.
Validation: 366 passed, 6 platform symlink skips; Ruff lint/format and mypy passed.
The [005 umbrella](005-archiver-foundation.md) is partially implemented; remaining
children require their own plans and activation. Earlier archive-reservation
and no-implementation statements below are historical.

**Implemented (2026-11-08):** [002.3 — Catalog Scan History Pruning](002.3-catalog-scan-history-pruning.md)
adds explicit transactional retention, per-scan API/CLI summaries, and opt-in
old-failure cleanup. All acceptance criteria and completion checks passed.
Plan 005 remains reserved for archive work; no subsequent plan is active.

[Bugfix 006 — Reconciliation path ordering](../bugfixes/006-reconciliation-path-ordering.md)
is implemented and validated (2026-11-08).
[002.2 — Refresh Hash Verification](002.2-refresh-hash-verification.md)
is implemented (2026-11-08), including API/CLI verification, tests, and documentation.
[004.9 — Duplicate Pagination](004.9-duplicate-pagination.md)
is implemented (2026-11-07), including API/CLI pagination and the public tutorial.
Umbrella [004](004-query-grammar-and-api-refactor.md), including the 004.9
extension, is implemented and reviewed at the handoff checkpoint below.

**Implemented (2026-11-08):** [002.2 — Refresh Hash Verification](002.2-refresh-hash-verification.md)
adds an explicit full-verification mode while preserving incremental refresh,
and records mandatory verification for future archive ingestion. All acceptance
criteria and completion checks passed. Plan 005 is reserved for archive work;
no subsequent plan is active.

**Implementation handoff (2026-09-30):** 004.4.1 and 004.5 are implemented and
approved. The user also reviewed and approved the duplicate group streaming
and query/stream lifecycle notebooks on 2026-09-30. The
[final API contract](004-public-api-target.md) and 004.6–004.8 plans remain
prepared for the accepted direction. No subsequent plan is active. Do not begin
004.6a automatically.

**Readiness review (2026-09-30):** [004.6](004.6-existing-read-api-migration.md)
is ready to begin with 004.6a on an implementation request. Its four children
retain sequential gates. Plans now account for the completed duplicate-stream
lifecycle, pinned detail reads, assertion-preserving migration, regex cleanup,
and deferred public-tutorial corrections that do not block migration. Baseline validation:
186 tests passed, 3 skipped; Ruff lint/format and mypy passed. This review
updates planning only and does not activate or implement a child.

004.4 introduced grouped queries conservatively; actual use later revealed
why groups need sorting and explicit streaming; 004.4.1 evolves that abstraction
in response to a demonstrated requirement. Its activation follows completion
of 004.5 despite its parent-based numbering.

## Catalog review and archive handoff — 2026-11-08

At the user's explicit request, all existing catalog plans, supporting contract/
notebook plans, and bug-fix plans are marked **implemented and reviewed**.
This checkpoint supersedes older pending-review/pending-commit headers and
historical readiness gates. Earlier dates, validation outcomes, and historical
checklists remain recorded; they are not a description of outstanding work.
No implementation plan is currently active.

- Reviewed scope: Plans 001–004 and their existing subplans, including 002.2,
  002.3, 004.9, the final API contract, and bug-fix plans 001–006.
- Git milestone: annotated tag `catalog-reviewed-archive-ready`, pointing to
  the commit containing this review record and the updated plan statuses.
- Catalog baseline: schema version 2, read-only source observation, incremental
  and full-verification refresh, content-level tags, current/history queries,
  duplicate inspection, explicit history pruning, API and CLI documentation.
- Most recent code validation (Plan 002.3, commit `d79fde1`): 333 tests passed,
  5 platform symlink skips; Ruff lint/format, mypy, and diff checks passed.
  This handoff changes documentation only and does not claim another code audit.
- Plan 005 is reserved for archive implementation. The next implementation must
  first define explicit managed-storage authority, source/target direction,
  duplicate/conflict policy, failure recovery/publication, and mandatory fresh
  SHA-256 verification of copied destination bytes against expected identity.
  A cached source hash or prior catalog verification cannot replace it.

The catalog is accepted as the reviewed foundation for archive work. Archive
implementation has not started and is not included in this tag. Retention keeps
its documented limits: close readers before pruning, preserve newer failures
and running scans, and perform any optional physical compaction separately.

## Numbering

A top-level plan uses a whole number such as `001`. Follow-up work that extends that plan uses a decimal subplan number such as `001.1`, `001.2`, and `001.3`.

Each subplan must identify its parent plan and include created, implementation,
and status fields. Preserve historical implementation records. Explicitly
requested parent-plan amendments must be dated and link their detailed
subplans; this does not retroactively rewrite earlier implementation outcomes.

## Timeline

| Sequence | Plan | Status | Parent | Summary |
| --- | --- | --- | --- | --- |
| 005 | [Archiver Foundation](005-archiver-foundation.md) | In progress; 005.1 completed | - | Catalog prerequisites, one-way sync, and local deduplication in six gated children. |
| 005.1 | [Targeted Catalog Updates and Inventory Retention](005.1-targeted-catalog-updates.md) | Implemented; awaiting review | 005 | Snapshot-copying updates behind a publication boundary; retain two managed snapshots by default. |
| 001 | [Catalog Foundation](001-catalog-foundation.md) | Implemented (reviewed) | - | SQLite-backed read-only cataloging, current state, and duplicate discovery. |
| 001.1 | [Streaming Catalog History Query APIs](001.1-streaming-history-query-apis.md) | Implemented (reviewed) | 001 | Typed, streaming scan and observation history for debugging and exploration. |
| 001.2 | [Pandas DataFrame Adapters](001.2-pandas-dataframe-adapters.md) | Implemented (reviewed) | 001 | Flat DataFrame adapters for typed current, history, and duplicate-group query results. |
| 001.3 | [Summary-First Catalog CLI](001.3-catalog-cli.md) | Implemented (reviewed) | 001 | Small in-root catalog CLI for initialization, scanning, status, and duplicate summaries. |
| 001.4 | [Streaming Scan Progress](001.4-streaming-scan-progress.md) | Implemented (reviewed) | 001 | Interactive, single-pass progress feedback for CLI scans without a pre-count. |
| 001.5 | [Bounded Current-File Browsing](001.5-bounded-current-file-browsing.md) | Implemented (reviewed) | 001 | Bounded, sortable current-file exploration with aggregate matched size. |
| 002 | [Catalog Refresh and Reconciliation](002-catalog-refresh-and-reconciliation.md) | Implemented (reviewed) | 001 | Read-only filesystem reconciliation with typed changes and atomic snapshot application. |
| 002.1 | [Refresh CLI Preview and Application](002.1-refresh-cli-preview-and-application.md) | Implemented (reviewed) | 002 | Dry-run reconciliation previews, detailed path reporting, and compatible refresh commands. |
| 002.2 | [Refresh Hash Verification](002.2-refresh-hash-verification.md) | Implemented (reviewed) | 002 | Incremental refresh defaults, explicit full SHA-256 verification, and a mandatory future-ingestion verification contract. |
| 002.3 | [Catalog Scan History Pruning](002.3-catalog-scan-history-pruning.md) | Implemented (reviewed) | 002 | Explicit root-scoped successful-scan retention, dry-run reporting, atomic deletion, and preserved inventory/content/tags. |
| 003 | [Bounded Duplicate Inspection](003-bounded-duplicate-inspection.md) | Implemented (reviewed) | 001 | Bounded duplicate-group and member inspection with complete aggregate metrics. |
| 003.1 | [Catalog Safety and Boundary Test Expansion](003.1-catalog-safety-and-boundary-tests.md) | Implemented (reviewed) | 003 | Regression coverage for transactional, persistence, filesystem, hashing, location, refresh, and CLI safety boundaries. |
| 003.2 | [Content-Level Tags](003.2-content-level-tags.md) | Implemented (reviewed) | 003 | Provenance-aware content tags, explicit schema migration, and bounded tag queries. |
| 003.3 | [Bounded Tag and File Search](003.3-bounded-tag-and-file-search.md) | Implemented (reviewed) | 003.2 | Shared design boundary for bounded content, file, and vocabulary search. |
| 003.3a | [Multi-Tag Content Search](003.3a-bounded-tag-query-apis.md) | Implemented (reviewed) | 003.3 | End-to-end bounded multi-tag content API and `tags find` CLI. |
| 003.3b | [Tag-Aware Current-File Search](003.3b-tag-aware-file-and-content-cli.md) | Implemented (reviewed) | 003.3 | End-to-end tag-aware current-file API and compatible `catalog files` CLI. |
| 003.3c | [Available Tag Vocabulary](003.3c-tag-vocabulary-cli.md) | Implemented (reviewed) | 003.3 | End-to-end active vocabulary usage API and `tags available` CLI. |
| 004 | [Query Grammar and API Refactor](004-query-grammar-and-api-refactor.md) | Implemented (reviewed) | - | Incremental typed query grammar and one compiled read-query architecture. |
| 004.1 | [Query Foundation](004.1-query-foundation.md) | Implemented (reviewed) | 004 | Minimal immutable query description, SQLite compilation, and execution boundary for `current_files()`. |
| 004.2 | [Current Observation API](004.2-current-observation-api.md) | Implemented (reviewed) | 004 | Public bounded current-file query with typed path, sort, and limit expressions. |
| 004.3 | [Materialization and Reductions](004.3-materialization-and-reductions.md) | Implemented (reviewed) | 004 | Current-file terminals, complete reductions, bounded streaming, and explicit unbounded streaming. |
| 004.4 | [Grouped Queries and Duplicates](004.4-grouped-queries-and-duplicates.md) | Implemented (reviewed) | 004 | Bounded current duplicate groups with complete summaries and member expansion; retire the Phase 1 duplicate query facade. |
| 004.4.1 | [Duplicate Group Sorting and Streaming](004.4.1-duplicate-group-sorting-and-streaming.md) | Implemented (reviewed) | 004.4 | Typed sorting and explicit bounded/unbounded group and member streams for later duplicate-handling workflows. |
| 004.5 | [Tag and Content Predicates](004.5-tag-and-content-predicates.md) | Implemented (reviewed) | 004 | Shared predicates and distinct-content reduction. |
| 004.6 | [Existing Read API Migration](004.6-existing-read-api-migration.md) | Implemented (reviewed) | 004 | Four sequential migration increments. |
| 004.6a | [File Details and Migration](004.6a-file-details-and-migration.md) | Implemented (reviewed) | 004.6 | Typed tag views and direct files CLI queries. |
| 004.6b | [Content Queries and Details](004.6b-content-queries-and-details.md) | Implemented (reviewed) | 004.6 | Catalog-wide content with root-scoped path previews. |
| 004.6c | [Tag Vocabulary Queries](004.6c-tag-vocabulary-queries.md) | Implemented (reviewed) | 004.6 | Active vocabulary, usage counts and regex. |
| 004.6d | [Retained Read Compilation](004.6d-retained-read-compilation.md) | Implemented (reviewed) | 004.6 | Shared internal execution for exact/history reads. |
| 004.7 | [Public API Review and Cleanup](004.7-public-api-review-and-cleanup.md) | Implemented (reviewed) | 004 | Public member/export audit, complete Catalog reference, and tutorial compatibility cleanup. |
| 004.8 | [CLI Consistency Audit](004.8-cli-consistency-audit.md) | Implemented (reviewed) | 004 | CLI contract audit, integration coverage, CLI reference, and umbrella completion review. |
| 004.9 | [Duplicate Pagination](004.9-duplicate-pagination.md) | Implemented (reviewed) | 004 | Immutable group offsets, bounded CLI pages, live refresh semantics, tests and documentation. |
