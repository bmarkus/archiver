# Design Evolution Notes

This document records how Archiver's read API has evolved. It explains why
earlier choices made sense, what pressure those choices exposed, and why the
project is introducing a query grammar in small, reviewable steps.

The account is intentionally historical. It does not treat the current design
as inevitable, and it does not turn planned grammar features into promises.

## Start with concrete operations

Archiver began with a small set of domain ideas: content is identified by bytes,
file instances are observations at locations, scans preserve history, and a
location's current state comes from its latest successful scan.

The first API reflected the questions the catalog could already answer:

~~~python
search_current_files(...)
search_duplicate_groups(...)
search_content_by_tags(...)
~~~

This was a good starting point. Each operation had a clear purpose, direct SQL,
and focused tests. More importantly, the project did not need to invent a
general abstraction before it had evidence about the operations users needed.

Concrete APIs also made the domain boundaries visible. Paths describe
observations and are scoped to locations; they are not content identity.
Duplicate inspection is read-only. A failed scan cannot become current state.
Those rules remain true regardless of how queries are represented.

## The pressure for a shared model

As the catalog grew, new read operations increasingly combined concepts that
already existed: current state, path filtering, tags, sorting, grouping,
counting, and aggregate values.

Tag-aware current-file search made the pattern clear. It resembled ordinary
current-file search, with tag filtering and tag output added. Continuing this
way would either create more specialized functions or consolidate them into
large functions with many optional arguments.

A single all-purpose function might look like this:

~~~python
file_search(
    root=...,
    glob=...,
    tags=...,
    tag_match="all",
    provenance=...,
    include_tags=True,
    sort_by=...,
)
~~~

That reduces the number of method names, but it does not reduce complexity.
The combinations of options still need clear semantics, validation, and tests.
The complexity merely moves into conditionals inside one method.

The implementation also had another source of duplication: several public
operations contained table names, joins, grouping rules, and SQL ordering. A
schema or relationship change could therefore touch several unrelated API
methods. The relational work cannot disappear, but it can have one well-defined
home.

## Introduce a typed query boundary

The emerging structure is:

~~~text
CLI and domain API
        ↓
typed query description
        ↓
compiler
        ↓
SQLite database
~~~

A query description expresses catalog semantics, not SQL fragments, column
names, or arbitrary Python callbacks. The compiler translates validated
descriptions into parameterized SQLite queries and materializes the resulting
domain objects.

This boundary has two benefits. Public code composes familiar domain vocabulary,
while database details remain behind the compiler. It also gives the project one
place to define ordering, bounds, result shape, and safety rules.

The boundary does not replace the domain API. Catalog methods remain useful as
semantic starting points. They create a query for a particular domain, while the
query grammar supplies the reusable transformations.

## The grammar available today

Plans 004.1 through 004.4 established two focused query domains: current file
observations and duplicate-content groups for one root.

~~~python
from archiver.query import path, size

query = catalog.files(root)
query = query.where(path.glob("photos/*.jpg"))
query = query.sort(size.desc())
query = query.limit(20)
rows = query.fetch()
~~~

Duplicate inspection changes the result grain and therefore has its own bounded
query type:

~~~python
groups = catalog.duplicates(root).limit(20).member_limit(20).fetch()
summary = catalog.duplicates(root).summary()
~~~

This remains intentionally limited. Current-file queries support path GLOB
filtering, deterministic ordering, bounded and streaming materialization, and
complete reductions. Duplicate queries support fixed content grouping,
bounded groups and members, and complete summaries. Queries are immutable and
lazy: each transformation returns a new description, and terminals resolve the
root's latest successful scan.

The [query grammar](docs/query-grammar.md) is the public usage reference.
[Plan 004](docs/plans/004-query-grammar-and-api-refactor.md) records the
implementation sequence and architectural constraints.

## Why result shape matters

The current grammar preserves the meaning of a row: it returns file
observations. Some future catalog questions will have a different result shape.

For example, duplicate inspection groups observations by content identity. A
duplicate-group result contains a content identity, copy count, aggregate byte
count, and bounded members, while an individual relative path is no longer the
primary result. Its distinct typed result domain rejects operations that apply
only to current-file rows.

This is one reason the grammar is being added gradually. Grouping, aggregation,
historical observations, tag predicates, and additional materialization
operations each need semantics that are specific enough to preserve the
catalog's invariants. They are not just new SQL clauses.

## Keep the design incremental

The lesson is not that the early specialized APIs were wrong. They revealed the
actual patterns and constraints that a more general interface must respect.

The query grammar should grow only when existing behavior provides a concrete
reason to add vocabulary. Each subplan can then define its public API, result
types, ordering, bounds, compiler behavior, tests, and compatibility story
without pretending to design the complete language in advance.

That approach keeps Archiver's central promises intact: content identity remains
independent of paths, current state comes from successful scans, queries are
read-only, and catalog results stay deterministic and bounded where their
interface requires it.
