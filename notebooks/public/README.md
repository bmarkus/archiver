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
| [Catalog query and stream lifecycle](query-stream-lifecycle.ipynb) | Explains catalogs, reusable queries, lazy cursor-backed streams, independent group/member cursors, retained handles, scan pinning, and cleanup. |

The current public API reference is [the query grammar](../../docs/query-grammar.md).
Plan-specific notebooks link to their associated implementation plans from
within the notebook where relevant.

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
| Catalog query and stream lifecycle | **Yes — approved** | The user explicitly reviewed and approved this notebook on 2026-09-30; recorded in [Plan 004.4.1](../../docs/plans/004.4.1-duplicate-group-sorting-and-streaming.md#review-and-completion). |
