# Phase 1 — Catalog Foundation

Phase 1 developed Archiver from a minimal file catalog into a small but coherent model for observing and reasoning about file collections.

The work began with a few foundational decisions: content is identified independently of pathname; scans are immutable observations; the latest successful scan defines current state; and historical information should be preserved rather than overwritten. From these principles came the SQLite catalog, SHA-256 content identity, scan history, filesystem reconciliation, bounded file and duplicate inspection, and content-level tags with provenance and retraction history.

Development was intentionally incremental. New capabilities were first exposed as explicit catalog methods and CLI commands, with tests and notebooks used to make their semantics visible. This worked well while the set of operations was small and helped establish the domain model and its safety boundaries.

As the catalog became more expressive, however, filtering, matching, sorting, projection, bounding, and aggregation began to recur in different combinations. Current-file queries, duplicate queries, content/tag queries, and tag-aware file queries increasingly required their own specialized APIs and SQL implementations.

This is not a failure of Phase 1; it is its main architectural result. The concrete APIs made the common operations visible.

Phase 2 therefore starts by asking whether these operations can become a small composable query grammar: a vocabulary in which catalog sources are transformed by reusable operations and compiled into SQL. The goal is to preserve the semantics established in Phase 1 while replacing the growing collection of special-purpose query interfaces with a simpler underlying structure.

## Frozen reference

The `phase/1-catalog-foundation` branch preserves the repository at the end of Phase 1. It is intended for review and historical study and should not receive further development changes.