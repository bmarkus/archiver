# Current-file query grammar

This document describes the supported public grammar for finding file
observations from a catalog's latest successful scan. It is a usage reference,
not an implementation plan.

Import the catalog and field vocabulary:

~~~python
from archiver import Catalog
from archiver.query import mtime, path, size
~~~

Start a query for one filesystem root with Catalog.files(root). The root is
validated and canonicalized when the query is created. The query is lazy: it
does not look up a scan or read observations until fetch() runs.

~~~python
query = catalog.files(root)
~~~

Every new query has ascending relative-path order and a limit of 20 rows.

## Grammar

~~~python
catalog.files(root)
    .where(path.glob(pattern))  # zero or more times
    .sort(ordering)             # zero or more times
    .limit(count)               # zero or more times
    .fetch()
~~~

where(), sort(), and limit() each return a new query. They never mutate the
earlier query, so a base query can be reused to create several variants.

| Method | Accepted value | Effect |
| --- | --- | --- |
| where(...) | path.glob(pattern) | Add a path predicate. |
| sort(...) | path.asc(), path.desc(), size.asc(), size.desc(), mtime.asc(), or mtime.desc() | Replace the ordering. |
| limit(...) | A positive int | Replace the maximum result count. |
| fetch() | No arguments | Return matching FileObservation values as a tuple. |

There is intentionally no public unbounded fetch. To change the result bound,
call limit(count) before fetch().

## Filtering with path.glob()

where() currently accepts only predicates created by path.glob(pattern).
Passing a string directly, such as where("*.txt"), is not supported.

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
`FileObservation` or `None` when no row matches. `count()` and
`total_size_bytes()` return the complete number and logical size of matching
current observations; neither reduction is affected by `limit()`.

```python
first_row = preview.first()
match_count = preview.count()
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

## Current boundary

The current grammar supports path GLOB filtering; path, size, and modification
time ordering; positive limits; bounded fetching and streaming; explicit
unbounded streaming; and complete count and size reductions. It does not yet
support size or time predicates, content or tag predicates, OR or NOT
composition, projection, grouping, pagination, or historical-observation
queries.