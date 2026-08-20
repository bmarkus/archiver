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
| 001.4 | [Streaming Scan Progress](001.4-streaming-scan-progress.md) | Planned (active) | 001 | Interactive, single-pass progress feedback for CLI scans without a pre-count. |

