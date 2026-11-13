# Public notebooks

These notebooks are disposable tutorials and design demonstrations for
Archiver. They use synthetic data unless a notebook explicitly says otherwise.
Run them from the repository root so the project environment and relative
documentation links resolve correctly:

```powershell
uv run jupyter lab notebooks/public
```

Committed notebooks should have no saved outputs or execution counts. A clean
execution validates that a notebook runs; it does not by itself mean that the
notebook has been reviewed or approved.

## Compatibility

Current tutorials: refresh/reconciliation, Plan 004.2 through 004.6, and query/stream
lifecycle, plus Plan 004.9 duplicate pagination, Plan 005.1 targeted updates,
Plan 005.2 cross-catalog comparison, and Plan 005.3 operation workspaces.
The Plan 004 query-grammar playground is a standalone conceptual model,
not the production API. The catalog API and query grammar below are authoritative.

Historical tutorials use retired APIs and require a separate frozen checkout:

| Tutorials | Required revision |
| --- | --- |
| Plans 001, 003, 003.2, 003.3a–c, and 004 managed-operations motivation | `7082066b5c97ab18fc488f00b5c745b5f23478b1` (Phase 1) |
| Plan 004.1 internal foundation | `c1f12d1` |

Run `uv sync` and launch Jupyter from the matching checkout. Each historical
notebook also carries this warning in its opening cell. Frozen tutorials preserve
the teaching context of their original API; they are not current usage examples.

## Notebook guide

| Notebook | Brief description |
| --- | --- |
| [Catalog refresh and reconciliation](catalog-refresh-and-reconciliation.ipynb) | Builds a disposable catalog, previews filesystem changes, and applies a refresh while explaining reconciliation and current-state behavior. |
| [Plan 001 catalog tour](plan-001-catalog-tour.ipynb) | Introduces the catalog foundation: scanning, content identity, current observations, history, and duplicate discovery. |
| [Plan 003 bounded duplicate inspection](plan-003-bounded-duplicate-inspection.ipynb) | Explores summary-first duplicate inspection with deterministic group/member limits and no destructive duplicate handling. |
| [Plan 003.2 content-level tags](plan-003.2-content-level-tags.ipynb) | Demonstrates provenance-aware tag assertions and retractions attached to content identity rather than pathname. |
| [Plan 003.3a multi-tag content search](plan-003.3a-multi-tag-content-search.ipynb) | Searches content using multiple active tags and explains matching, provenance, bounded previews, and complete totals. |
| [Plan 003.3b tag-aware current-file search](plan-003.3b-tag-aware-current-file-search.ipynb) | Combines current paths, content-level tags, tag-aware file filtering, CLI examples, and current-scan isolation. |
| [Plan 003.3c available tag vocabulary](plan-003.3c-available-tag-vocabulary.ipynb) | Inspects active tag names and usage counts with provenance, ordering, regex filtering, and CLI examples. |
| [Plan 004 managed-operations motivation](plan-004-managed-operations-motivation.ipynb) | Motivates later safe managed operations and previews prospective workflows without moving or deleting files. |
| [Plan 004 query-grammar playground](plan-004-query-grammar-playground.ipynb) | Explores a teaching-oriented query grammar model. Treat it as conceptual material rather than the current production API reference. |
| [Plan 004.1 query foundation](plan-004.1-query-foundation.ipynb) | Walks through the initial internal query description, compiler, and executor boundary while identifying private APIs explicitly. |
| [Plan 004.2 current observation API](plan-004.2-current-observation-api.ipynb) | Introduces the supported immutable public grammar for filtering, sorting, limiting, and fetching current observations. |
| [Plan 004.3 materialization and reductions](plan-004.3-materialization-and-reductions.ipynb) | Demonstrates bounded fetch/stream terminals, explicit unbounded streaming, first-row selection, and complete reductions. |
| [Plan 004.4 grouped duplicate queries](plan-004.4-grouped-queries-and-duplicates.ipynb) | Demonstrates bounded duplicate groups, bounded member previews, complete group metrics, summaries, and deterministic ordering. |
| [Plan 004.4.1 duplicate group streaming](plan-004.4.1-duplicate-group-streaming.ipynb) | Adds typed duplicate-group sorting and bounded or explicit unbounded group/member streaming with safe early closure. |
| [Plan 004.5 tag and content predicates](plan-004.5-tag-and-content-predicates.ipynb) | Composes exact content and active-tag predicates across current-file queries using the implemented public grammar. |
| [Plan 004.6 existing read API migration](plan-004.6-existing-read-api-migration.ipynb) | Covers current-file details, catalog-wide content details, active tag vocabulary, retained exact/history reads, and migrated CLI parity. |
| [Plan 004.9 duplicate pagination](plan-004.9-duplicate-pagination.ipynb) | Fetches groups 11–20 and 91–100 through API/CLI, streams suffixes, and demonstrates refresh shifts and retained scan-pinned handles. |
| [Plan 005.1 targeted Catalog updates](plan-005.1-targeted-catalog-updates.ipynb) | Registers additions, updates content, retires catalog paths, retains two snapshots, and performs a full refresh in a temporary sandbox. |
| [Plan 005.2 cross-catalog content comparison](plan-005.2-cross-catalog-content-comparison.ipynb) | Compares temporary inventories with complete summaries, bounded classified batches, deterministic representatives, safe reader cleanup, and stale-file/current-inventory examples. |
| [Plan 005.3 operation workspaces](plan-005.3-operation-workspaces.ipynb) | Explains saved sync work lists in plain language: small batches, counts, closing/reopening, recording a pretend failure, inspecting an unfinished attempt, and explicit discard without copying files. |
| [Catalog query and stream lifecycle](query-stream-lifecycle.ipynb) | Explains catalogs, reusable queries, lazy cursor-backed streams, independent group/member cursors, retained handles, scan pinning, and cleanup. |

Current references: [Catalog API](../../docs/catalog-api.md) and
[query grammar](../../docs/query-grammar.md).
Plan-specific notebooks link to their associated implementation plans from
within the notebook where relevant.

## Execution audit — 2026-11-07

All 17 notebooks passed nbformat validation and clean-kernel execution. Current
notebooks ran against current source; historical notebooks ran against their
stated frozen revision. Output stripping was verified without retaining execution
artifacts. This validation does not change the user-review records below.

## Status

“Reviewed” below means the associated plan explicitly records a review after
including the notebook in its implementation outcome. “No review record” means
that execution or validation may be documented, but this repository does not
record a notebook review. Validation and review are intentionally separate.

| Notebook | Reviewed already? | Evidence or current state |
| --- | --- | --- |
| Catalog refresh and reconciliation | No review record | Associated [Plan 002](../../docs/plans/002-catalog-refresh-and-reconciliation.md) records implementation, but no explicit notebook review marker. |
| Plan 001 catalog tour | No review record | The Phase 1 material is historical; no notebook-specific review marker was found. |
| Plan 003 bounded duplicate inspection | No review record | Associated [Plan 003](../../docs/plans/003-bounded-duplicate-inspection.md) records implementation, but no explicit notebook review marker. |
| Plan 003.2 content-level tags | No review record | Associated [Plan 003.2](../../docs/plans/003.2-content-level-tags.md) records implementation, but no explicit notebook review marker. |
| Plan 003.3a multi-tag content search | No review record | The associated plan is approved, but it does not explicitly record notebook review. |
| Plan 003.3b tag-aware current-file search | No review record | Clean execution is recorded in [Plan 003.3b](../../docs/plans/003.3b-tag-aware-file-and-content-cli.md), but notebook review is not. |
| Plan 003.3c available tag vocabulary | No review record | The notebook is recorded as a post-implementation demonstration; notebook review is not recorded. |
| Plan 004 managed-operations motivation | No review record | Prospective motivation artifact; no notebook review marker was found. |
| Plan 004 query-grammar playground | No review record | Conceptual playground; no notebook review marker was found. |
| Plan 004.1 query foundation | **Yes** | [Plan 004.1](../../docs/plans/004.1-query-foundation.md) records review on 2026-09-03 and includes the notebook in its validated outcome. |
| Plan 004.2 current observation API | **Yes** | [Plan 004.2](../../docs/plans/004.2-current-observation-api.md) records review on 2026-09-06 and includes the notebook in its validated outcome. |
| Plan 004.3 materialization and reductions | **Yes** | [Plan 004.3](../../docs/plans/004.3-materialization-and-reductions.md) records review on 2026-09-08 and includes the notebook in its validated outcome. |
| Plan 004.4 grouped duplicate queries | **Yes** | [Plan 004.4](../../docs/plans/004.4-grouped-queries-and-duplicates.md) records review on 2026-09-09 and includes the notebook in its validated outcome. |
| Plan 004.4.1 duplicate group streaming | **Yes — approved** | The user explicitly reviewed and approved this notebook on 2026-09-30; recorded in [Plan 004.4.1](../../docs/plans/004.4.1-duplicate-group-sorting-and-streaming.md#review-and-completion). |
| Plan 004.5 tag and content predicates | **Yes** | [Plan 004.5](../../docs/plans/004.5-tag-and-content-predicates.md) records review on 2026-09-29 and includes the notebook in its validated outcome. |
| Plan 004.6 existing read API migration | No review record | This notebook is the post-implementation tutorial for Plans 004.6a–d; execution and validation are recorded in the implementation plan. |
| Catalog query and stream lifecycle | **Yes — approved** | The user explicitly reviewed and approved this notebook on 2026-09-30; recorded in [Plan 004.4.1](../../docs/plans/004.4.1-duplicate-group-sorting-and-streaming.md#review-and-completion). |
| Plan 004.9 duplicate pagination | No review record | All eight code cells passed clean-kernel execution, nbformat validation and output stripping on 2026-11-07; user review is not recorded. |
| Plan 005.2 cross-catalog content comparison | No review record | All nine code cells passed two fresh-kernel executions and nbformat validation on 2026-11-13; saved outputs/counts are empty. User review is not recorded. |
| Plan 005.3 operation workspaces | No review record | All sixteen code cells passed two fresh-kernel executions and nbformat validation on 2026-11-13; saved outputs/counts are empty. Validation is recorded in Plan 005.3; user review is not recorded. |

## Plan 005.1 tutorial — 2026-11-12

[Targeted Catalog updates and snapshot retention](plan-005.1-targeted-catalog-updates.ipynb)
uses only temporary files and a new schema-3 database. It includes assertions for
additions, replacements, catalog-only removals, retention of two successful
snapshots, a subsequent full refresh, close/reopen persistence, keep-all override,
and cleanup. It contains no Plan 005.2 work. Execution validation is recorded in
[Plan 005.1](../../docs/plans/005.1-targeted-catalog-updates.md); user review has not
yet been recorded.
