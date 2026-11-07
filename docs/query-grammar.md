# Public query grammar

This document describes the supported public grammar for current files,
duplicate-content groups, catalog-wide content, and active tag vocabulary. It
is a usage reference, not an implementation plan. For lifecycle, scanning,
exact/history reads, mutations, and migration, see the [Catalog API reference](catalog-api.md).

The [Plan 004.5 public tutorial](../notebooks/public/plan-004.5-tag-and-content-predicates.ipynb)
demonstrates content and tag predicates with disposable synthetic files.

The topic-oriented [catalog query and stream lifecycle tutorial](../notebooks/public/query-stream-lifecycle.ipynb)
demonstrates lazy execution, reusable queries, independent cursors, retained
duplicate handles, scan pinning, and explicit cleanup. It connects the stream
contracts introduced by Plans 004.3 and 004.4.1.

## Query vocabulary

The objects exported by `archiver.query` are **public vocabulary namespaces**.
They do not contain catalog data or execute queries. Their methods create
immutable, typed predicate or ordering expressions that queries validate and
compile later. Expressions can be created without an open catalog and reused
across compatible queries.

```python
from archiver.query import content, duplicate, mtime, path, size, tags
```

| Object | Expressions produced | Query domain |
| --- | --- | --- |
| `path` | `glob(pattern)`, `asc()`, `desc()` | Current files |
| `size` | `asc()`, `desc()` | Current files and duplicate groups |
| `mtime` | `asc()`, `desc()` | Current files |
| `content` | `is_(content_id)` | Current files and catalog-wide content |
| `tags` | `all_of(*names, provenance=None)`, `any_of(*names, provenance=None)` | Current files and catalog-wide content |
| `duplicate` | `file_count.asc()`, `file_count.desc()`, `redundant_bytes.asc()`, `redundant_bytes.desc()` | Duplicate groups |
| `tag_name` | `regex(source)`, `asc()`, `desc()` | Tag vocabulary |
| `tag_usage` | `content_count.asc()/desc()`, `assertion_count.asc()/desc()` | Tag vocabulary |

Pass predicates to `where()` and orderings to `sort()`. `path.glob()` matches
the complete POSIX relative path; `content.is_()` matches a complete `ContentId`;
and the `tags` methods match active content tags. `asc()` and `desc()` select
ascending and descending order. `size` means observed file size for current
files and the size of one content identity for duplicate groups. `mtime` means
observed filesystem modification time. The nested `duplicate.file_count` and
`duplicate.redundant_bytes` namespaces order groups by their complete file
instance count and potential redundant bytes, respectively.

```python
text_paths = path.glob("*.txt")
largest_first = size.desc()

files = catalog.files(root).where(text_paths).sort(largest_first)
groups = catalog.duplicates(root).sort(largest_first)
most_copies = catalog.duplicates(root).sort(duplicate.file_count.desc())
```

These statements only build expressions and queries. A terminal such as
`fetch()` executes a query; streaming terminals execute when iteration begins.
Expression factories validate their arguments when called, and queries check
that the expressions belong to their supported domain. For example,
`duplicate.file_count.desc()` cannot order current files, and `path.asc()`
cannot order duplicate groups. Duplicate queries do not accept predicates.
Use these public methods rather than constructing expression classes directly.

## Current files

Import the catalog and field vocabulary:

~~~python
from archiver import Catalog
from archiver.query import content, mtime, path, size, tags
~~~

Start a query for one filesystem root with Catalog.files(root). The root is
validated and canonicalized when the query is created. The query is lazy: it
does not look up a scan or read observations until a terminal runs.

~~~python
query = catalog.files(root)
~~~

Every new query has ascending relative-path order and a limit of 20 rows.

## Grammar

~~~python
catalog.files(root)
    .where(predicate)           # zero or more times
    .sort(ordering)             # zero or more times
    .limit(count)               # zero or more times
    .fetch()
~~~

where(), sort(), and limit() each return a new query. They never mutate the
earlier query, so a base query can be reused to create several variants.

| Method | Accepted value | Effect |
| --- | --- | --- |
| where(...) | path.glob(pattern), content.is_(content_id), tags.all_of(...), or tags.any_of(...) | Add a predicate. |
| sort(...) | path.asc(), path.desc(), size.asc(), size.desc(), mtime.asc(), or mtime.desc() | Replace the ordering. |
| limit(...) | A positive int | Replace the maximum result count. |
| fetch() | No arguments | Return matching FileObservation values as a tuple. |

There is intentionally no public unbounded fetch. To change the result bound,
call limit(count) before fetch().

## Filtering

`where()` accepts typed predicates created by the public `path`, `content`, and
`tags` factories. Passing raw strings, content IDs, or tag names directly is
not supported.

### Paths

Patterns use SQLite's case-sensitive GLOB syntax and match the complete POSIX
relative path stored in the catalog. Paths do not include the root itself and
always use /, including on Windows.

| Pattern | Matches |
| --- | --- |
| "*.txt" | Every .txt path, including paths below nested directories. |
| "nested/*" | Every path whose relative path begins with nested/. |
| "nested/*.txt" | .txt paths below nested/. |
| "images/??.jpg" | Paths such as images/a1.jpg with exactly two characters before .jpg. |
| "[a-c]*.txt" | .txt paths whose first character is a, b, or c. |

In SQLite GLOB, * matches zero or more characters and ? matches one character.
A slash has no special wildcard boundary, so * can match slashes. Use character
classes such as [a-z] when needed. SQLite GLOB has no escape character; avoid
treating these patterns as regular expressions or as Python pathlib glob
patterns.

Each additional where() is combined with logical AND:

~~~python
text_under_nested = catalog.files(root).where(path.glob("nested/*")).where(path.glob("*.txt"))
~~~

The example returns only paths that satisfy both patterns.

### Exact content identity

Use `content.is_()` with a complete `ContentId` to select every current path
whose bytes have that identity:

```python
from archiver import ContentId

content_id = ContentId("sha256", "a" * 64)
copies = catalog.files(root).where(content.is_(content_id))
```

Both the algorithm and digest participate in matching. Content identity is
independent of pathname, so duplicate paths with the same bytes all match.

### Active tags

Use `tags.all_of()` when every named tag must be active on the content, or
`tags.any_of()` when at least one must be active:

```python
reviewed_photos = catalog.files(root).where(tags.all_of("photo", "reviewed"))
interesting = catalog.files(root).where(tags.any_of("favorite", "important"))
system_photos = catalog.files(root).where(tags.all_of("photo", provenance="system"))
```

Tag names are validated, deduplicated, and placed in canonical order. At least
one name is required. `provenance` may be `None` (the default), `"user"`, or
`"system"`. With `all_of()`, every requested tag must have an independently
matching active assertion under that provenance. Retracted assertions do not
match, and multiple active producers do not duplicate file results.

Repeated `where()` calls always mean logical AND, including across predicate
families:

```python
reviewed_nested = catalog.files(root).where(tags.all_of("reviewed")).where(path.glob("nested/*"))
```

## Sorting and limiting

Use an ordering expression created by one of the public fields:

~~~python
largest_first = catalog.files(root).sort(size.desc())
oldest_first = catalog.files(root).sort(mtime.asc())
reverse_path = catalog.files(root).sort(path.desc())
~~~

size is the observed file size in bytes. mtime is the observed filesystem
modification time, not scan time and not content identity. For equal sizes or
modification times, ascending relative path is used as a deterministic
tie-breaker.

Calling sort() again replaces the previous ordering. Calling limit() again
replaces the previous bound. Filtering always applies before sorting, and
sorting always applies before limiting, regardless of the order in which the
methods were called.

~~~python
preview = catalog.files(root).where(path.glob("*.txt")).sort(size.asc()).limit(10)
~~~

limit() accepts only positive integers. The result bound is applied before
observations are materialized.

## Terminals

Call fetch() with no arguments to materialize the query:

~~~python
rows = preview.fetch()
for row in rows:
    print(row.relative_path, row.size_bytes, row.mtime_ns)
~~~

The result is a reusable tuple of FileObservation values containing at most the
query's limit. fetch() resolves the latest successful scan at execution time, so
a query created before a root is scanned can be fetched after a later successful
scan. A valid root that has never been scanned, or is not known to the catalog,
returns an empty tuple.

Use `first()` for one ordered result without materializing a page. It returns a
`FileObservation` or `None` when no row matches. `count()`,
`distinct_content_count()`, and `total_size_bytes()` return the complete file
count, content-identity count, and logical size of matching current
observations; none of these reductions is affected by `limit()`.

```python
first_row = preview.first()
match_count = preview.count()
content_count = preview.distinct_content_count()
total_bytes = preview.total_size_bytes()
```

`stream()` yields typed observations in the query's deterministic order and
honors its positive bound. It holds an SQLite cursor while it is consumed, so
the catalog must remain open until iteration finishes or the iterator is
closed.

```python
for row in preview.stream():
    print(row.relative_path)
```

`stream_all()` is the deliberate unbounded terminal. It keeps filtering and
ordering but removes the row bound. It is appropriate only when the caller
intends to consume every matching observation; there is no `fetch_all()`.

```python
for row in preview.stream_all():
    archive_every_row(row)
```

Each terminal resolves current state independently. If a scan changes the
catalog between terminals, separate calls may observe different successful
current scans; queries do not create snapshots.

## Duplicate groups

Use `Catalog.duplicates(root)` to inspect content identities with more than one
file instance in the root's latest successful scan.

The [Plan 004.4.1 public tutorial](../notebooks/public/plan-004.4.1-duplicate-group-streaming.ipynb)
provides a complete disposable sorting and streaming example.

A minimal query looks like this:

```python
from archiver import Catalog, DuplicateGroupQuery
from archiver.query import duplicate, size

query: DuplicateGroupQuery = catalog.duplicates(root).sort(size.desc())
groups = query.limit(20).member_limit(20).fetch()
summary = query.summary()
```

New duplicate queries default to at most 20 groups and 20 displayed members per
group. These are Python API defaults. The CLI `--details` defaults to group
metrics only (`--member-limit 0`); a positive CLI member limit opts into filenames
through the existing bounded fetch. The API still requires positive member
limits. `limit()` and `member_limit()` replace those bounds immutably and accept
only positive integers. `sort()` accepts `size.asc()/desc()`,
`duplicate.file_count.asc()/desc()`, or
`duplicate.redundant_bytes.asc()/desc()`. Repeated sorting replaces the earlier
ordering. Redundant bytes descending is the default.

`fetch()` returns a tuple of `DuplicateGroupView` values. Each view contains the
full content identity, content size, complete file-instance count, complete
potential redundant bytes, and its bounded member tuple. Potential redundant
bytes are `(file_instance_count - 1) * size_bytes`; empty-file duplicate groups
are retained with zero potential redundant bytes.

Every group ordering uses complete content algorithm and digest ascending as
the deterministic tie-breaker, including descending primary order. Members are
always ordered by complete POSIX relative path ascending.

`summary()` returns a `DuplicateSummary` over every duplicate group and ignores
both display bounds. It contains the complete duplicate-group count,
duplicate-file-instance count, and potential redundant bytes. Unknown,
unscanned, and duplicate-free roots return no groups and an all-zero summary.

`stream()` yields at most the group limit as `DuplicateGroupHandle` values;
`stream_all()` explicitly removes only the group limit. Handles contain the
same complete metrics as views but no eager member tuple. Their
`stream_members()` terminal honors the inherited member limit, while
`stream_all_members()` explicitly removes only that member limit.

```python
from contextlib import closing

with closing(query.stream_all()) as groups:
    for group in groups:
        with closing(group.stream_all_members()) as members:
            for member in members:
                print(group.content_id, member.relative_path)
```

Group and member streams are generator-backed and hold independent SQLite
cursors. Close partially consumed iterators explicitly with `contextlib.closing`;
close member and group streams before closing the catalog. Closing a group
stream does not close a separately opened member stream.

Each group terminal resolves the latest successful scan once on first
advancement. Every yielded handle remains pinned to that scan, including after
the outer iterator closes or a later successful refresh. Separate terminals
may resolve different scans. Group iteration never fetches members; a member
cursor opens only when its own iterator first advances.

The earlier `duplicate_groups()`, `search_duplicate_groups()`,
`duplicate_summary()`, and tuple-oriented `duplicate_groups_frame()` interfaces
were retired when this grouped query was introduced. The duplicate CLI uses
this query directly.

## Duplicate pagination

`DuplicateGroupQuery.offset(count: int) -> Self` skips zero-based group positions
before applying the group limit. The default offset is 0. Transformations are
immutable: repeated `offset()` replaces the offset, and `offset(0)` resets it.
Ordering, offset, and limit have the same semantics regardless of call order.

```python
query = catalog.duplicates(root)
query.offset(10).limit(10).fetch()  # groups 11–20; positions [10, 20)
query.offset(90).limit(10).fetch()  # groups 91–100; positions [90, 100)
```

Offsets require exact integers from 0 through `2**63 - 1`; booleans, floats,
strings, and `None` raise `TypeError`, while negative/overflow values raise
`ValueError`. Validation is eager. Group rows are ordered and paged in SQL;
Python does not collect or iterate through skipped groups. Deep offsets may
still require SQLite to aggregate, sort, and traverse earlier groups.

`fetch()` and `stream()` honor the offset and group limit. `stream_all()` honors
the offset but removes the group limit, yielding the remaining suffix. Use
`offset(0).stream_all()` for every group. Offsets never affect member paths,
member bounds, or complete per-group metrics. `summary()` ignores pagination.
Offsets at or beyond the group count return empty pages; final pages can be partial.
Existing ordering and full content-identity ascending tie-breakers are unchanged.

Each fresh terminal resolves the latest successful scan (streams on first
advancement). On an unchanged scan and ordering, adjacent pages have no overlap
or omissions. Successful refreshes can shift positions, so sequential requests
across refreshes can repeat or skip content identities. Failed/interrupted scans
do not change the current state. A started stream and its handles retain their
resolved scan, including member reads after refresh or outer-stream closure.
Row numbers are temporary positions; full `ContentId` is identity.

Avoid successful refreshes during a review requiring stable page positions.
There is no pinned paging session, historical scan selector, or new snapshot API.
CLI summaries and rows remain separate terminals and may describe different
successful scans during a concurrent refresh. See the
[CLI page contract](catalog-cli.md#duplicate-pages) and
[executable pagination tutorial](../notebooks/public/plan-004.9-duplicate-pagination.ipynb).

## Imports, validation, and detail result types

Import query classes and domain/result records from `archiver`; import expression
factories from `archiver.query`. Construct queries through `Catalog`, not private
constructor arguments. Query transformations validate eagerly: wrong value types
raise `TypeError`, unsupported values raise `ValueError`; numeric limits exclude
`bool`. Invalid roots raise `ScanFailure` and malformed regexes retain `re.error`.
All terminals require an open catalog; stream execution begins on first advancement.

| Query class (`archiver`) | Materialized result |
| --- | --- |
| `CurrentFileQuery` | `tuple[FileObservation, ...]` |
| `CurrentFileTagQuery` | `tuple[CurrentFileTagView, ...]` |
| `DuplicateGroupQuery` | `tuple[DuplicateGroupView, ...]` |
| `ContentQuery` | `tuple[ContentRecord, ...]` |
| `ContentDetailQuery` | `tuple[ContentTagView, ...]` |
| `TagVocabularyQuery` | `tuple[TagUsage, ...]` |

All queries default to 20 parent rows/groups. Detail views retain their base
query’s parent bound and expose `fetch()`; content details additionally expose
`current_path_count() -> int`. Set transformations on the base before creating
a view. The base keeps its original row type. `all_tags` requires a boolean;
`True` removes only the per-content tag preview bound, never the parent bound.
Numeric preview limits remain positive integers even when all tags are requested;
`None` is rejected. Displayed tags span all active provenance regardless of matching
provenance. Path previews are full relative POSIX paths ascending.

## File tag details

`catalog.files(root).with_tags(limit=3)` returns a `CurrentFileTagQuery` whose
`fetch()` result contains `CurrentFileTagView` rows. Parent filtering, ordering,
and bounds come from the base file query. Each selected content identity gets a
complete active-tag count and an alphabetical preview; duplicate paths reuse
the same preview. `all_tags=True` removes only the per-content preview bound.

## Catalog-wide content

`catalog.contents()` returns a bounded `ContentQuery` over every persisted
content identity, including untagged and historical-only content. It accepts
`content.is_(...)`, `tags.all_of(...)`, and `tags.any_of(...)`, has a default
limit of 20, fixed full-identity ascending order, `fetch()`, and complete
`count()`.

Use `with_details(root=..., path_limit=20, tag_limit=3, all_tags=False)` for a
`ContentDetailQuery`. The root scopes only current path counts/previews; content
selection and active-tag previews remain catalog-wide. `current_path_count()`
counts paths for every matching identity and ignores display bounds.

## Active tag vocabulary

`catalog.tags()` returns a bounded `TagVocabularyQuery` over canonical names
with active assertions. `where(tag_name.regex(source))` uses Python
`re.search`; repeated regex predicates are ANDed. Provenance scopes both
inclusion and usage counts. Sorting supports tag name, distinct-content usage,
and assertion usage. `fetch()` defaults to 20 names and `count()` returns the
complete matching-name count. `provenance(None)` resets to all producers; repeated
`provenance()` calls replace that scope. Usage-order ties use name ascending,
including descending primary order. Multiple regex filters use AND.

## Current boundary

The current-file grammar supports path GLOB filtering; path, size, and
modification-time ordering; positive limits; bounded fetching and streaming;
explicit unbounded streaming; and complete count and size reductions. The
duplicate grammar supports one fixed content grouping, typed group ordering,
bounded fetch previews, bounded and explicit unbounded group/member streams,
group offsets, and a complete summary. It does not support duplicate predicates,
configurable member ordering, OR or NOT composition, projection, arbitrary grouping,
member pagination, keyset pagination, or historical-observation queries.

## CLI mapping

The [Catalog CLI reference](catalog-cli.md) records which public queries back each
command, their display bounds and ordering, and preserved error behavior. The CLI
uses typed query rows and explicit totals; it does not expose every library
transformation or streaming terminal.
