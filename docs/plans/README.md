# Implementation Plans

Plans record the evolution of Archiver's design and implementation.

## Numbering

A top-level plan uses a whole number such as `001`. Follow-up work that extends that plan uses a decimal subplan number such as `001.1`, `001.2`, and `001.3`.

Each subplan must identify its parent plan and include created, implementation, and status fields. The parent plan remains an immutable record of its original scope; subplans record later decisions and implementation work.

## Timeline

| Sequence | Plan | Status | Parent | Summary |
| --- | --- | --- | --- | --- |
| 001 | [Catalog Foundation](001-catalog-foundation.md) | Foundation | - | SQLite-backed read-only cataloging, current state, and duplicate discovery. |
| 001.1 | [Streaming Catalog History Query APIs](001.1-streaming-history-query-apis.md) | Implemented (pending commit) | 001 | Typed, streaming scan and observation history for debugging and exploration. |
| 001.2 | [Pandas DataFrame Adapters](001.2-pandas-dataframe-adapters.md) | Implemented (pending commit) | 001 | Flat DataFrame adapters for typed current, history, and duplicate-group query results. |
| 001.3 | [Summary-First Catalog CLI](001.3-catalog-cli.md) | Implemented (pending commit) | 001 | Small in-root catalog CLI for initialization, scanning, status, and duplicate summaries. |
| 001.4 | [Streaming Scan Progress](001.4-streaming-scan-progress.md) | Implemented (pending commit) | 001 | Interactive, single-pass progress feedback for CLI scans without a pre-count. |
| 001.5 | [Bounded Current-File Browsing](001.5-bounded-current-file-browsing.md) | Implemented (pending commit) | 001 | Bounded, sortable current-file exploration with aggregate matched size. |
| 002 | [Catalog Refresh and Reconciliation](002-catalog-refresh-and-reconciliation.md) | Implemented (pending commit) | 001 | Read-only filesystem reconciliation with typed changes and atomic snapshot application. |
| 002.1 | [Refresh CLI Preview and Application](002.1-refresh-cli-preview-and-application.md) | Implemented (pending commit) | 002 | Dry-run reconciliation previews, detailed path reporting, and compatible refresh commands. |
| 003 | [Bounded Duplicate Inspection](003-bounded-duplicate-inspection.md) | Implemented (pending commit) | 001 | Bounded duplicate-group and member inspection with complete aggregate metrics. |
| 003.1 | [Catalog Safety and Boundary Test Expansion](003.1-catalog-safety-and-boundary-tests.md) | Implemented (pending commit) | 003 | Regression coverage for transactional, persistence, filesystem, hashing, location, refresh, and CLI safety boundaries. |
| 003.2 | [Content-Level Tags](003.2-content-level-tags.md) | Implemented (pending commit) | 003 | Provenance-aware content tags, explicit schema migration, and bounded tag queries. |
| 003.3 | [Bounded Tag and File Search](003.3-bounded-tag-and-file-search.md) | Umbrella design record | 003.2 | Shared design boundary for bounded content, file, and vocabulary search. |
| 003.3a | [Multi-Tag Content Search](003.3a-bounded-tag-query-apis.md) | Implemented (pending review) | 003.3 | End-to-end bounded multi-tag content API and `tags find` CLI. |
| 003.3b | [Tag-Aware Current-File Search](003.3b-tag-aware-file-and-content-cli.md) | Planned | 003.3 | End-to-end tag-aware current-file API and compatible `catalog files` CLI. |
| 003.3c | [Available Tag Vocabulary](003.3c-tag-vocabulary-cli.md) | Planned | 003.3 | End-to-end active vocabulary usage API and `tags available` CLI. |

