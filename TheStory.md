# Design Evolution Notes

This document records how the Archiver design evolved as the implementation grew.

The goal is not only to describe the final architecture, but to preserve the reasoning that led to it. Archiver is also an educational project, and many useful design lessons only become clear when we can see why an earlier solution was reasonable, where it started to strain, and what new abstraction became necessary.

The first design deliberately started simple.

The catalog was modeled around a few clear domain ideas: files are observations at paths, content is identified independently from paths, scans preserve history, and the current state is derived from the latest successful scan. The API then exposed concrete operations for the questions we already knew how to ask.

This produced functions such as:

```python
search_current_files(...)
search_duplicate_groups(...)
search_content_by_tags(...)
```

This approach had several advantages:

* each operation had a clear purpose;
* the SQL could be written directly and optimized for that operation;
* behavior was explicit and easy to test;
* we did not need to invent abstractions before understanding the problem.

This was intentional. Early in a project, a general framework can easily encode the wrong assumptions. Implementing concrete use cases first allowed the actual query patterns and domain relationships to emerge from working code.

As the catalog gained more capabilities, however, the API began to show a repeated pattern. New requirements were often not completely new operations. They were combinations of concepts that already existed: current state, path filtering, tags, sorting, grouping, counting, and aggregation.

For example, adding tag-aware file search produced a new function that was largely the existing file search plus tag filtering and tag output. Similar SQL relationships and query-building logic began appearing in several places.

At this point there were two obvious ways to continue.

One was to keep adding specialized API functions. Another was to consolidate them into a few large functions with many optional arguments and conditional branches.

Both approaches could work, but both would move the same growing complexity to different places.

At the same time, many API functions were becoming tightly coupled to the physical SQL schema. A change to tables or relationships could therefore require modifications across several otherwise unrelated operations.

This was the point where we stopped adding features and reconsidered the structure.

The existing implementation had done its job: it had exposed enough real use cases to reveal a more general pattern. Continuing to tidy the API would risk spending effort organizing an interface whose underlying model was already changing.

The emerging idea is to introduce a small query grammar between the public interfaces and SQL:

```text
CLI / domain API
        ↓
query grammar
        ↓
compiler
        ↓
SQL database
```

The grammar does not replace the lessons learned from the first implementation. It is built from them.

The following sections document that transition step by step: what problem appeared, why the earlier solution was reasonable, what alternatives were considered, and why the project eventually chose a different structure.

This document should continue to grow in the same way as the project itself. Each major design change can be added as another short story rather than rewriting the history to make the final architecture look inevitable.


# Why the API Moved Toward a Query Grammar

The first catalog API was built in the simplest possible way: add a function for each useful operation.

For example:

```python
search_current_files(...)
search_current_files_with_tags(...)
search_content_by_tags(...)
search_available_tags(...)
search_duplicate_groups(...)
```

This worked well at first. Each function was explicit, easy to test, and safe.

## The first problem: similar functions started multiplying

`search_current_files()` and `search_current_files_with_tags()` are a good example.

The second function is mostly the first one plus:

* filtering by tags;
* choosing `all` or `any` tag matching;
* optionally returning tag information.

This raised an obvious question: why create another function? We could instead keep one large API:

```python
file_search(
    root=...,
    glob=...,
    tags=...,
    tag_match="all",
    provenance=...,
    include_tags=True,
    sort_by=...,
)
```

This avoids having many functions.

But the complexity has only moved inside the function:

```python
if tags:
    ...

if tag_match == "all":
    ...

if provenance:
    ...

if include_tags:
    ...

if sort_by == "size":
    ...
```

As new features are added, the difficult part becomes the interaction between all these options.

So there are two forms of the same problem:

```text
many small API functions
        or
few very large functions with many conditionals
```

## The second problem: API functions know too much SQL

Many catalog functions directly contain table names, joins, CTEs, grouping rules, and column names.

That means a database-schema change can require changes in many unrelated API functions.

For example, adding tag filtering to a file search requires understanding:

```text
file observation
      ↓
content
      ↓
tag assertion
      ↓
tag
```

This relationship has to be implemented somewhere whether or not we use a grammar.

The important question is therefore not:

> Can we avoid this complexity?

We cannot.

The better question is:

> Where should this complexity live?

A compiler gives it one home.

```text
API / CLI
    ↓
query description
    ↓
compiler
    ↓
SQL schema
```

The user can write:

```python
tags.has("picture")
```

while the compiler decides whether that requires `EXISTS`, a join, a CTE, or another strategy.

## The grammar idea

Instead of creating functions for combinations of features, define a few reusable query operations:

```python
q = (
    catalog.files(root)
    .where(path.glob("photos/**"))
    .where(tags.has_all("family", "picture"))
    .sort(size.desc())
)
```

The query is lazy. It only describes what should happen.

The compiler later turns that description into SQL.

New requirements usually add new vocabulary rather than new API functions.

When tags are introduced, for example, we add:

```python
tags.has(...)
tags.has_all(...)
tags.has_any(...)
```

plus the compiler rules needed to implement them.

`where()`, `sort()`, `fetch()`, and the rest of the query machinery do not need to change.

## Aggregation changes what a result means

Some operations are different because they change the meaning of a row.

For example:

```python
catalog.files(root)
```

represents files.

But:

```python
(
    catalog.files(root)
    .group_by(content)
    .summarize(
        copies=count(),
        redundant_bytes=(count() - 1) * size,
    )
)
```

represents duplicate-content groups.

After that operation, fields such as:

```text
content
copies
redundant_bytes
```

exist, while `path` may no longer make sense.

The query object can carry this information as metadata and reject invalid operations.

## Why still keep an API?

A completely open grammar is powerful, but it also makes it easy to write nonsense or accidentally rebuild important domain concepts incorrectly.

So useful operations can still have safe wrappers:

```python
q = catalog.duplicates(root)
```

Internally this wrapper is only a predefined grammar expression. It contains no special SQL.

The important difference is that it still returns a query object.

For example:

```python
q = catalog.duplicates(root)

q.fields
# content
# copies
# size
# redundant_bytes
```

The result can then be extended safely:

```python
q.where(redundant_bytes > 1_000_000)
q.sort(redundant_bytes.desc())
q.sum(redundant_bytes)
```

while something meaningless can be rejected:

```python
q.sort(path)
# error: path is not available for duplicate groups
```

This example is a strong reason not to spend too much effort merely tidying the existing API.

A cleaned-up API would still eventually need to represent:

* composable operations;
* changing result shapes;
* available fields;
* validation;
* schema-independent SQL generation.

At that point we would be slowly rebuilding a grammar anyway.

## Resulting structure

The emerging design is therefore:

```text
CLI
    constrained, user-friendly commands

Domain API
    safe wrappers such as duplicates()

Query grammar
    reusable composition and validation

Compiler
    translates domain expressions into SQL

Database
    physical schema
```

The lesson from the first implementation is not that specialized APIs were a mistake. They were the simplest way to discover the real requirements.

Their growth revealed the deeper structure of the problem.

Once that structure became visible, moving directly to a grammar became more useful than continuing to reorganize an API whose complexity would otherwise keep reappearing in new forms.
