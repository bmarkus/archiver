# Plan 004 — Query Grammar and API Refactor

**Status:** Approved  
**Related plans:** [001.1](001.1-streaming-history-query-apis.md), [001.5](001.5-bounded-current-file-browsing.md), [003](003-bounded-duplicate-inspection.md), [003.2](003.2-content-level-tags.md), [003.3](003.3-bounded-tag-and-file-search.md)  
**Sequence:** 004  
**Created:** 2026-09-03  
**Approved:** 2026-09-03

## Goal

Direction amended 2026-09-29: [004.4.1](004.4.1-duplicate-group-sorting-and-streaming.md)
is active after completion of 004.5. It extends duplicate groups with typed
sorting and explicit bounded/unbounded streaming of groups and members to
support future duplicate-handling workflows. Mutation workflows remain outside
this query refactor; implementation of the extension has not started.

Implementation direction amended 2026-09-09 at the user's request. The
[final public API contract](004-public-api-target.md) and prepared 004.5–004.8
subplans below specify the accepted target. This is planning only; no next
subplan starts until explicitly requested. Earlier implementation records
remain historical.

Refactor the catalog read API around a small composable query grammar while preserving existing catalog semantics and behavior.

The current API has accumulated specialized functions and result types for combinations of current and historical observations, path/content/tag filtering, sorting and limiting, duplicate detection, and aggregation. Many repeat the same relational operations. Replace those parallel query paths incrementally with:

1. a small set of immutable query descriptions;
2. a typed vocabulary of composable operations;
3. a few named domain-level entry points;
4. a compiler and execution boundary responsible for efficient SQLite queries.

Here, **grammar** means a typed Python query model, not a textual language or parser. Query objects describe catalog semantics and must not expose SQL fragments, database column names, or unrestricted Python callbacks as public predicates.

## Domain alignment

The query model must preserve the domain model and invariants:

- content identity remains byte-based and distinct from pathname;
- every observed path remains scoped to a location;
- current state comes only from that location's latest successful scan;
- historical observations retain their scan identity and status;
- duplicates remain read-only groups by content identity within one root's current state;
- tag predicates use content-level, active, provenance-aware assertion semantics;
- query execution grants no authority to mutate source files.

This plan does not redesign catalog meaning. Existing tests and approved plans are the behavioral reference.

## Architectural boundaries

### Query representation

Immutable objects describe what the caller wants. They contain domain semantics rather than SQL or SQLite-specific artifacts.

The initial observation-preserving pipeline is conceptually:

```text
observation source → current scope → filter → sort → limit
```

Historical and current observations are distinct result domains. A historical row retains scan context; a current row is scoped to one root and its latest successful scan.

Projection is not assumed to preserve the observation type. If `select` is later justified by an existing use case, it creates an explicitly typed projected query. It is not part of the initial row-preserving vocabulary.

### Query vocabulary

Operations are added only when existing behavior requires them. Likely concepts include `filter`, `sort`, `limit`, `group`, and `aggregate`; these are not commitments to exact public signatures.

Predicates and sort expressions must be typed and validated. Calling order and logical meaning must be unambiguous: each subplan must define canonical clause semantics or reject surprising operation orders.

### Domain API

`Catalog` provides four semantic query constructors in the intended final API:

```python
catalog.files(root)
catalog.duplicates(root)
catalog.contents()
catalog.tags()
```

`files` means current file observations scoped to one root. `duplicates`
means current duplicate-content groups at that root. `contents` is catalog-wide
content, including identities without current paths. `tags` is active vocabulary
and usage, independent of locations. Historical reads retain their explicit
iterator APIs. Contents and tags constructors are planned, not implemented.

Semantic constructors should compose the shared query representation. A useful combination of existing operations should normally not require another `Catalog` method; new methods should represent genuine domain concepts.

### Compilation and execution

A backend compiler translates validated descriptions into efficient parameterized SQL and materializes typed results. A general query model does not justify naive execution or catalog-wide materialization.

Query construction is lazy. Unless a later subplan explicitly introduces snapshots, a query is evaluated when a terminal operation executes, requires its catalog to remain open, and separate terminals need not observe one shared snapshot if catalog state changes between them.

The compiler must preserve SQL-side filtering, aggregation, deterministic ordering, and bounding where existing APIs require them.

## Result shape and grain

Row-preserving operations retain the source query's result domain. Operations that change row shape or grain create another typed query or terminal result:

```text
observations → group / aggregate → grouped or scalar result
```

Grouping is not complete merely because the compiler can emit `GROUP BY`. Duplicate inspection is the first grouped use case and includes:

- grouping current observations by full content identity;
- requiring more than one file instance;
- complete aggregate metrics across all matching groups;
- bounded group rows;
- bounded, deterministically ordered members within each group.

The design must represent that contract without a separate duplicate-query engine.

## Materialization, bounds, and ordering

Query construction and materialization are separate. Terminal operations may include collection, first result, existence, count, and domain-specific totals when justified by current behavior.

The public API must not make accidental unbounded materialization the easy default. Each materializing terminal must preserve an applicable bound or require an explicit request for unbounded consumption. Existing streaming history behavior must remain available.

Counts, totals, and duplicate summaries describe the complete matching set, not only collected rows, and must be computed efficiently by SQLite.

Whenever a limit affects an ordered public result, define the effective ordering and deterministic tie-breakers. A query with no inherent ordering must not acquire accidental directory- or database-enumeration ordering.

## Compatibility

This read/query refactor preserves:

- current-state and historical-observation semantics;
- duplicate semantics and aggregate definitions;
- content identity;
- tag assertion, retraction, matching, and provenance semantics;
- mutation APIs and transaction behavior;
- existing CLI selection, bounds, summaries, and deterministic output unless a subplan explicitly approves a visible change.

Existing methods may first delegate to the grammar as compatibility wrappers. Removing, renaming, or incompatibly changing a public API requires an explicit decision in an approved subplan, normally the public API cleanup subplan, including a deprecation or versioning policy where appropriate. Plan 004.4 explicitly retires the Phase 1 duplicate query facade and its tuple-oriented pandas adapter rather than retaining parallel public shapes around the new grouped query. One internal architecture does not itself authorize any other breaking public change.

## Schema and safety boundary

No persistent schema change is expected. A subplan must separately justify and version any schema change before implementation.

All work remains read-only with respect to source files and catalog metadata on successful and failing query paths. This plan does not add archive ingest, duplicate resolution, tag mutation, merge policy, working-copy management, remote storage, GUI behavior, background watchers, or Rust code.

## Implementation sequence

Each subplan must be independently understandable, testable, and approved before implementation. The final contract defines only the vocabulary required by existing capabilities; it does not authorize a general grammar or automatic implementation of the sequence.

### 004.1 — Query Foundation

Introduce the minimal immutable internal query representation and the boundary between query description, SQLite compilation, and execution. Route existing `Catalog.current_files(root)` behavior through the new path without changing its public API.

**Completion goal:** prove that one current-observation query can be represented and executed while preserving behavior exactly.

### 004.2 — Current Observation API

Introduce `Catalog.files(root)`, typed path/sort expressions, immutable `where`/`sort`/`limit` transformations, and one bounded `fetch()` terminal needed to make the first public query useful.

**Completion goal:** existing current-file filtering, sorting, and bounding can be expressed through the public query API.

### 004.3 — Materialization and Reductions

Expand materialization beyond the bounded `fetch()` established in 004.2. Define first-result behavior, counts, simple totals, streaming where applicable, and explicit unbounded-consumption rules.

**Completion goal:** one filtered query can support existing materializations without specialized search-result engines.

### 004.4 — Grouped Queries and Duplicates

Introduce the first grain-changing operation, using duplicate inspection to define group predicates, computed values, complete summaries, bounded groups, and bounded member expansion.

Retire the Phase 1 `duplicate_groups()`, `search_duplicate_groups()`, and
`duplicate_summary()` methods, their composite search result, and the
tuple-oriented duplicate DataFrame adapter rather than retaining parallel
public shapes around the grouped query. Map the duplicate CLI directly to the
new query in this subplan while preserving its output.

**Completion goal:** duplicate inspection is expressed through the grammar without a separate implementation path.

### 004.5 — Tag and Content Predicates

Implement [004.5](004.5-tag-and-content-predicates.md): typed all/any tag predicates,
exact content identity predicates and complete distinct-content counts on files.
Preserve active-assertion, content-level, multi-tag, and provenance semantics.

**Completion goal:** current observations can be filtered by existing tag semantics through the grammar.

### 004.6 — Existing Read API Migration

Follow [004.6](004.6-existing-read-api-migration.md) in four children: file detail
views and caller migration; catalog-wide content queries and details; active tag
vocabulary; retained exact/history read compilation. Retire replaced methods
and their search envelopes in the same increment as their CLI/caller migration.
No compatibility wrappers are needed for the explicitly listed pre-1.0 removals.

**Completion goal:** the repository has one internal query architecture rather than old and new engines in parallel.

### 004.7 — Public API Review and Cleanup

Follow [004.7](004.7-public-api-review-and-cleanup.md): audit actual public members
against the accepted classification, clean up obsolete exports, document the
implemented surface, and complete the public tutorial migration audit. No
unlisted breaking changes are authorized.

**Completion goal:** the public API is coherent, documented, and has an explicit migration story.

### 004.8 — CLI Consistency Audit

Follow [004.8](004.8-cli-consistency-audit.md): audit the CLI after its branches
have migrated with their owning API subplans. Preserve its bounded interface,
outputs and error behavior. The CLI need not expose every grammar operation.

Duplicate CLI migration belongs to 004.4; files, content search and vocabulary
belong to 004.6a–c. Retained exact reads and commands remain ordinary API calls.

**Completion goal:** CLI query behavior derives from the same model as the Python API.

## Intended end state

Subject to the public naming review, typical use should be conceptually similar to:

```python
catalog.files(root)

catalog.files(root).where(...).sort(...).limit(...).fetch()

catalog.duplicates(root).limit(20).member_limit(20).fetch()

catalog.contents().where(tags.all_of("favorite")).limit(20).fetch()

catalog.tags().where(tag_name.regex("^project:")).limit(20).fetch()
```

Internally:

```text
semantic API
      ↓
typed query expressions
      ↓
validated immutable representation
      ↓
SQLite compiler and executor
      ↓
typed materialization
```

Success means replacing specialized internal query implementations with a small, understandable set of concepts while preserving safety, clarity, determinism, bounds, and efficient execution. Expressiveness beyond demonstrated catalog use cases is not a goal.

## Completion

Plan 004 is complete only when its approved subplans have met their acceptance criteria and the plan index records their outcomes. Implement and review one subplan at a time. Do not begin a subsequent subplan automatically.
