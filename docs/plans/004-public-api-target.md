# Plan 004 — Final Public API Contract

**Status:** Implemented (reviewed).

**Review checkpoint:** 2026-11-08 — Reviewed at the user's request for the
catalog-to-archive handoff. Earlier dated implementation/review notes and
checklists remain historical; this checkpoint supersedes earlier pending
statuses and does not activate follow-on implementation. See the
[handoff record](README.md#catalog-review-and-archive-handoff--2026-11-08).

This document records the shared contract for Plans 004.5–004.8.
The [final umbrella review](004-query-grammar-and-api-refactor.md#final-implementation-review--2026-11-07)
accounts for its implementation; dated readiness notes below remain historical.
The domain model and invariants remain authoritative.

Amended 2026-09-29 by [004.4.1](004.4.1-duplicate-group-sorting-and-streaming.md):
typed duplicate sorting and explicit bounded/unbounded group and member streams
are implemented and approved as of 2026-09-30. Its detailed lifetime and bounds
contract governs those additions. Readiness review on 2026-09-30 clarified the
004.6 migration notes; its children remain prepared and unimplemented.

Amended 2026-11-07 by [004.9](004.9-duplicate-pagination.md): duplicate group
queries add immutable `offset(count: int) -> Self`, default zero, validated as
an exact integer in `0..2**63 - 1`. Group ordering precedes offset and limit;
`fetch()`/`stream()` honor both, and `stream_all()` removes only the limit while
retaining offset. Members are unaffected; complete summaries ignore pagination.
Fresh terminals use latest successful scan state; started streams and handles
stay pinned. Refreshes between pages may repeat/skip identities; there is no
paging snapshot/session. CLI `--group-offset` preserves offset-zero output and
uses one-based absolute numbers/ranges for nonzero pages. Earlier dated
implementation outcomes remain historical.

CLI display amended 2026-11-07 at the user's request:
[Bugfix 005](../bugfixes/005-duplicate-table-default.md) makes `duplicates --details`
show group metrics by default (`--member-limit 0`), using existing bounded group
handles without member queries. Positive CLI member limits retain previews.
Python API member limits/defaults remain unchanged. Prior dated default/output
records describe their historical checkpoint.

## Decisions and boundaries

Four public query constructors represent four result domains:
`Catalog.files(root)`, `Catalog.duplicates(root)`, `Catalog.contents()`,
and `Catalog.tags()`. Retained exact lookups and historical iterators remain
ordinary methods. Query descriptions, compilation, and materialization share
the existing private modules; do not introduce a generic query framework,
repository/service layer, SQL callbacks, schema change, or new dependency.

Construction and transformations are immutable and lazy, with eager argument
validation. Terminals require an open catalog. Root-taking constructors/views
validate and canonicalize the directory eagerly, then resolve persisted state
at execution. An unknown root is a valid directory absent from the catalog;
nonexistent, non-directory, and symlink roots retain existing validation errors.
Separate terminals may observe different state. Within a multi-statement
terminal, root-scoped observations use one resolved successful scan ID.

All numeric display limits are exact positive integers, excluding bool.
Wrong value types raise TypeError; unsupported values raise ValueError.
Invalid tag names use existing validation. Regex errors retain re.error.
Repeated where() calls mean AND; repeated sort()/limit() replace the clause.
Filtering precedes ordering and bounds regardless of transformation order.
All complete reductions ignore display limits. SQLite applies bounds before
materialization. SQL values are bound and identifiers come from closed mappings.

No new unbounded row/group collection is added. Explicit cursor-backed duplicate
group/member streams are authorized by 004.4.1; bounded fetch remains unchanged.
Existing current-file
stream_all(), history iterators, and exact per-content assertion lookups retain
their explicit scope. No mutation or filesystem authority changes are approved.

## Vocabulary

Export field/expression factories from archiver.query:

- Existing path.glob(), path.asc()/desc(), size.asc()/desc(), mtime.asc()/desc().
- 004.4.1: size.asc()/desc() also order duplicate content sizes;
  duplicate.file_count.asc()/desc() and duplicate.redundant_bytes.asc()/desc()
  order complete group metrics. Full algorithm/digest ascending breaks ties.
- content.is_(ContentId): exact full content identity; files and contents only.
- tags.all_of(*names, provenance=None) and tags.any_of(*names, provenance=None):
  files and contents only. Require at least one name, validate and deduplicate
  names, and canonicalize their order. Provenance is None, "user", or "system".
  Every requested tag in all_of must independently have a matching active
  assertion. Multiple producers must not multiply matches. Retractions exclude
  assertions from matching.
- tag_name.regex(source), tag_name.asc()/desc(): vocabulary only.
- tag_usage.content_count.asc()/desc() and
  tag_usage.assertion_count.asc()/desc(): vocabulary only.

Reject expressions from another domain. No general OR/NOT, arbitrary grouping,
size/time predicates, or duplicate predicates are included. Duplicate ordering
is limited to the expressions specified by 004.4.1.

## Query types and terminals

Define query types in archiver.query and re-export them from archiver.
Keep SQL/private tokens out of the documented construction API.

| Type | Source and transformations | Terminals and result |
| --- | --- | --- |
| CurrentFileQuery | files(root); where, sort, limit (default 20) | Existing fetch, first, count, total_size_bytes, stream, stream_all; add distinct_content_count() -> int |
| DuplicateGroupQuery | duplicates(root); sort, offset (default 0), limit, member_limit (both default 20); default redundant bytes descending | fetch() -> tuple[DuplicateGroupView, ...], summary() -> DuplicateSummary; 004.4.1 adds stream()/stream_all() -> Iterator[DuplicateGroupHandle] |
| ContentQuery | contents(); where, limit (default 20); fixed full algorithm/digest ascending | fetch() -> tuple[ContentRecord, ...], count() -> int; with_details(...) |
| TagVocabularyQuery | tags(); where(regex), sort, limit (default 20), provenance(kind) | fetch() -> tuple[TagUsage, ...], count() -> int |

ContentRecord is a frozen value with content_id: ContentId and size_bytes: int.
contents() starts from every persisted content identity, including untagged
content and content without current paths. Tag predicates narrow that set.

Vocabulary starts from distinct canonical names with active assertions anywhere
in the catalog. provenance(kind) replaces its scope (None resets to all);
scope affects both inclusion and all usage counts. Multiple regex predicates
mean AND with Python re.search semantics. Validate regex eagerly before SQL;
register a connection-local helper only for terminal execution and clean it
up in finally. No persistent or global regex state. Default name ascending;
count order ties use name ascending even for descending primary order.
count() counts matching names, not content or assertion rows.

## Typed detail views

004.4.1 separately adds immutable `DuplicateGroupHandle` in `archiver.query`,
re-exported from `archiver`. It carries complete group metrics and a pinned scan;
`stream_members()` respects the inherited member limit, while
`stream_all_members()` streams every member. Neither constructs a member tuple.
These handles are distinct from existing bounded `DuplicateGroupView` values.

CurrentFileQuery.with_tags(*, limit=3, all_tags=False) -> CurrentFileTagQuery.
ContentQuery.with_details(*, root, path_limit=20, tag_limit=3, all_tags=False)
-> ContentDetailQuery.

Views hold the immutable base query and display options. Both expose fetch();
ContentDetailQuery additionally exposes current_path_count() as specified below.
Set row filtering/order/bounds before creating
the view. To change them, derive another base query and create another view;
do not duplicate the whole transformation/terminal vocabulary on views.

- CurrentFileTagQuery.fetch() -> tuple[CurrentFileTagView, ...].
- ContentDetailQuery.fetch() -> tuple[ContentTagView, ...].
- ContentDetailQuery.current_path_count() -> int counts all current paths at
  its root for every matching content identity, ignoring all display bounds.
- File counts, distinct-content counts and sizes come from the base file query.
  Content count comes from the base content query.
- Displayed tags are distinct, alphabetical active names across ALL provenance,
  independent of matching provenance and requested tags. Complete active-tag
  counts remain available in each view.
- Content detail root controls path counts/previews only, never content
  selection. Unknown/unscanned roots give zero paths but retain matching
  catalog-wide content rows. Paths use full relative POSIX ascending order.
- all_tags must be bool. True removes only the per-content tag preview bound
  for the already bounded parent rows. Numeric limits remain validated but
  are ignored for that tag preview when all_tags=True; None is rejected.
  CLI retains mutually exclusive numeric/all-tags options.
- Fetch bounded parents first, then only their path/tag previews. Deduplicate
  selected content IDs for shared file tag previews. Reuse narrow helpers for
  active-tag counts/previews and FileObservation materialization.
- Pin root observation scan within each fetch. Do not promise atomic tag
  snapshots across statements or expose a new transaction/snapshot API.
  Separate totals and fetch calls, including those in one CLI command, remain
  separate terminals. Pinned scans preserve catalog observations, not live
  filesystem bytes. Preserve 004.4.1 stream/handle lifetimes during extraction.

## Catalog classification and migration owner

Replaced means the old entry point disappears after runtime callers and tests
use the specified replacement. Old notebooks do not block removal.
No deprecation shim or compatibility period is required for these
pre-1.0 changes. Preserve capabilities and errors unless this contract explicitly
changes a result shape or validation rule.

| Existing Catalog member | Decision | Replacement / owner |
| --- | --- | --- |
| create, open, migrate, close, __enter__, __exit__ | Retained | Lifecycle; unchanged |
| catalog_uuid, schema_version, database_path | Retained | Properties; unchanged |
| scan_directory | Retained | Convenient reconcile/apply; unchanged |
| reconcile_directory, apply_refresh | Retained | Preview/application; unchanged |
| files, duplicates | Retained | Query constructors |
| current_files | Replaced | files(root).stream_all(); collect explicitly if needed; 004.6a |
| search_current_files | Replaced | files query rows and reductions; 004.6a |
| find_by_content | Replaced | files(root).where(content.is_(id)); 004.6a |
| search_current_files_with_tags | Replaced | file predicates and with_tags(); 004.6a |
| search_tagged_content | Replaced | contents() predicate; tags_for_content(id) for assertions; 004.6b |
| search_content_by_tags | Replaced | contents() predicate and with_details(root=...); 004.6b |
| search_available_tags | Replaced | tags() vocabulary query; 004.6c |
| current_scan, current_summary | Retained | Exact single-result current state |
| scan_history, observation_history | Retained | Historical iterators, no fluent replacement |
| content_for_path | Retained | Exact resolution with existing missing-path error |
| tags_for_content, tags_for_path | Retained | Exact active assertion lookups |
| add_content_tag, add_tag_for_path | Retained | Existing mutations |
| retract_content_tag, retract_user_tag_for_path | Retained | Existing mutations |
| duplicate_groups, search_duplicate_groups, duplicate_summary | Removed in 004.4 | duplicates() |

## Types, adapters, and imports

Retain domain records, provenance types, errors, validation utilities, and the
three pandas adapters current_files_frame, observation_history_frame,
scan_history_frame. Keep pandas outside catalog/query execution.
Retain ContentTagView and CurrentFileTagView as detail result types.

Remove with their last caller: CurrentFileSearch, TagAwareCurrentFileSearch
(004.6a); TaggedContentSearch, TaggedContent, MultiTagContentSearch (004.6b);
AvailableTagSearch (004.6c). DuplicateGroupSearch and duplicate_groups_frame
were removed in 004.4. Retire CurrentFileSort, AvailableTagSort, TagMatchMode
when no public signature needs them; CLI may use local parsing types rather
than retain obsolete public aliases. Keep TagProvenanceKind and mutation types.
Removal ownership is CurrentFileSort in 004.6a, TagMatchMode in 004.6b, and
AvailableTagSort in 004.6c. Define ContentRecord in archiver.models and re-export
it from archiver alongside the other domain records.

## Consumer migration and history policy

Migrate a CLI branch in the same subplan that removes its catalog method.
Preserve arguments, defaults, output, totals, wrapping, error timing, and exit
codes, while moving query construction to the public grammar. Renderer helpers
receive typed rows and totals rather than recreated search-envelope objects.

Search tracked runtime source and tests for removed APIs. Preserve behavioral
tests by migrating them, not deleting their semantic coverage. Per user
clarification on 2026-09-30, old notebooks can be corrected later: their
compatibility and execution are not gates for API migration. Record deferred
corrections; retain no compatibility shims solely for notebook consumers.
Do not edit private notebooks, personal data, or historical implementation
records merely to remove textual mentions.

The 2026-09-30 inventory found retired duplicate calls in older tutorials.
These are documentation debt, not an implementation-readiness blocker.

## Shared completion gate

For every implementation subplan: run uv run pytest, uv run ruff check .,
uv run ruff format --check ., uv run mypy src tests, and git diff --check.
Include new/untracked files in whitespace inspection. Execute any edited public
notebook from a clean kernel, validate nbformat, and strip outputs/counts.
Update docs/query-grammar.md only as APIs become implemented. Record files,
tests, behavior, schema (none), decisions, deviations and validation outcomes.
Stop after the requested subplan; do not start the next automatically.
