# Implementation Plans

Plans record the evolution of Archiver's design and implementation.

**Active plan:** [004.4.1 — Duplicate Group Sorting and Streaming](004.4.1-duplicate-group-sorting-and-streaming.md)
(implementation not started).

**Implementation handoff (2026-09-29):** 004.5 is implemented and approved. The
[final API contract](004-public-api-target.md) and 004.6–004.8 plans remain
prepared for the accepted direction. 004.4.1 is now active for the demonstrated
duplicate-processing requirement; this handoff prepares its design, with
implementation awaiting a subsequent request. Do not begin 004.6a automatically.

004.4 introduced grouped queries conservatively; actual use later revealed
why groups need sorting and explicit streaming; 004.4.1 evolves that abstraction
in response to a demonstrated requirement. Its activation follows completion
of 004.5 despite its parent-based numbering.

## Numbering

A top-level plan uses a whole number such as `001`. Follow-up work that extends that plan uses a decimal subplan number such as `001.1`, `001.2`, and `001.3`.

Each subplan must identify its parent plan and include created, implementation,
and status fields. Preserve historical implementation records. Explicitly
requested parent-plan amendments must be dated and link their detailed
subplans; this does not retroactively rewrite earlier implementation outcomes.

## Timeline

| Sequence | Plan | Status | Parent | Summary |
| --- | --- | --- | --- | --- |
| 001 | [Catalog Foundation](001-catalog-foundation.md) | Foundation | - | SQLite-backed read-only cataloging, current state, and duplicate discovery. |
| 001.1 | [Streaming Catalog History Query APIs](001.1-streaming-history-query-apis.md) | Implemented (approved) | 001 | Typed, streaming scan and observation history for debugging and exploration. |
| 001.2 | [Pandas DataFrame Adapters](001.2-pandas-dataframe-adapters.md) | Implemented (approved) | 001 | Flat DataFrame adapters for typed current, history, and duplicate-group query results. |
| 001.3 | [Summary-First Catalog CLI](001.3-catalog-cli.md) | Implemented (approved) | 001 | Small in-root catalog CLI for initialization, scanning, status, and duplicate summaries. |
| 001.4 | [Streaming Scan Progress](001.4-streaming-scan-progress.md) | Implemented (approved) | 001 | Interactive, single-pass progress feedback for CLI scans without a pre-count. |
| 001.5 | [Bounded Current-File Browsing](001.5-bounded-current-file-browsing.md) | Implemented (approved) | 001 | Bounded, sortable current-file exploration with aggregate matched size. |
| 002 | [Catalog Refresh and Reconciliation](002-catalog-refresh-and-reconciliation.md) | Implemented (approved) | 001 | Read-only filesystem reconciliation with typed changes and atomic snapshot application. |
| 002.1 | [Refresh CLI Preview and Application](002.1-refresh-cli-preview-and-application.md) | Implemented (approved) | 002 | Dry-run reconciliation previews, detailed path reporting, and compatible refresh commands. |
| 003 | [Bounded Duplicate Inspection](003-bounded-duplicate-inspection.md) | Implemented (approved) | 001 | Bounded duplicate-group and member inspection with complete aggregate metrics. |
| 003.1 | [Catalog Safety and Boundary Test Expansion](003.1-catalog-safety-and-boundary-tests.md) | Implemented (approved) | 003 | Regression coverage for transactional, persistence, filesystem, hashing, location, refresh, and CLI safety boundaries. |
| 003.2 | [Content-Level Tags](003.2-content-level-tags.md) | Implemented (approved) | 003 | Provenance-aware content tags, explicit schema migration, and bounded tag queries. |
| 003.3 | [Bounded Tag and File Search](003.3-bounded-tag-and-file-search.md) | Implemented (approved) | 003.2 | Shared design boundary for bounded content, file, and vocabulary search. |
| 003.3a | [Multi-Tag Content Search](003.3a-bounded-tag-query-apis.md) | Implemented (approved) | 003.3 | End-to-end bounded multi-tag content API and `tags find` CLI. |
| 003.3b | [Tag-Aware Current-File Search](003.3b-tag-aware-file-and-content-cli.md) | Implemented (approved) | 003.3 | End-to-end tag-aware current-file API and compatible `catalog files` CLI. |
| 003.3c | [Available Tag Vocabulary](003.3c-tag-vocabulary-cli.md) | Implemented (approved) | 003.3 | End-to-end active vocabulary usage API and `tags available` CLI. |
| 004 | [Query Grammar and API Refactor](004-query-grammar-and-api-refactor.md) | Approved | - | Incremental typed query grammar and one compiled read-query architecture. |
| 004.1 | [Query Foundation](004.1-query-foundation.md) | Implemented (approved) | 004 | Minimal immutable query description, SQLite compilation, and execution boundary for `current_files()`. |
| 004.2 | [Current Observation API](004.2-current-observation-api.md) | Implemented (approved) | 004 | Public bounded current-file query with typed path, sort, and limit expressions. |
| 004.3 | [Materialization and Reductions](004.3-materialization-and-reductions.md) | Implemented (approved) | 004 | Current-file terminals, complete reductions, bounded streaming, and explicit unbounded streaming. |
| 004.4 | [Grouped Queries and Duplicates](004.4-grouped-queries-and-duplicates.md) | Implemented (approved) | 004 | Bounded current duplicate groups with complete summaries and member expansion; retire the Phase 1 duplicate query facade. |
| 004.4.1 | [Duplicate Group Sorting and Streaming](004.4.1-duplicate-group-sorting-and-streaming.md) | Active (implementation not started) | 004.4 | Typed sorting and explicit bounded/unbounded group and member streams for later duplicate-handling workflows. |
| 004.5 | [Tag and Content Predicates](004.5-tag-and-content-predicates.md) | Implemented (approved) | 004 | Shared predicates and distinct-content reduction. |
| 004.6 | [Existing Read API Migration](004.6-existing-read-api-migration.md) | Prepared umbrella | 004 | Four sequential migration increments. |
| 004.6a | [File Details and Migration](004.6a-file-details-and-migration.md) | Prepared | 004.6 | Typed tag views and direct files CLI queries. |
| 004.6b | [Content Queries and Details](004.6b-content-queries-and-details.md) | Prepared | 004.6 | Catalog-wide content with root-scoped path previews. |
| 004.6c | [Tag Vocabulary Queries](004.6c-tag-vocabulary-queries.md) | Prepared | 004.6 | Active vocabulary, usage counts and regex. |
| 004.6d | [Retained Read Compilation](004.6d-retained-read-compilation.md) | Prepared | 004.6 | Shared internal execution for exact/history reads. |
| 004.7 | [Public API Review and Cleanup](004.7-public-api-review-and-cleanup.md) | Prepared | 004 | Export, documentation and consumer audit. |
| 004.8 | [CLI Consistency Audit](004.8-cli-consistency-audit.md) | Prepared | 004 | End-to-end CLI contract and umbrella completion audit. |
