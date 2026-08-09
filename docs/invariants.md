# Invariants

These properties are intended to remain true across implementations and refactors.

## Identity

1. **Content identity is byte-based.** A file's pathname is never its content identity.
2. **Rename does not change content identity.** If bytes remain unchanged, moving or renaming a file does not create new content.
3. **Different paths may share content identity.** Duplicate physical copies are valid catalog observations.
4. **Same pathname does not imply same content.** A path may contain different bytes at different scans.

## Catalog observations

5. **Cataloging is read-only unless explicitly stated otherwise.** A scan must not modify, rename, move, delete, or rewrite source files.
6. **Filesystem metadata is observational.** Size, mtime, inode, device, and path may help describe or accelerate operations, but they are not content identity.
7. **Current state comes only from successful observations.** A failed or incomplete scan must not replace the last successful state.
8. **Historical scan identity is preserved.** Records must make it possible to distinguish observations made by different scans.
9. **Path representation is scoped to a location.** A relative path is meaningful only together with its location/root.

## Archive authority

10. **A catalog is not automatically an archive.** Catalog records alone do not grant authority to mutate physical storage.
11. **Managed storage must be explicit.** Destructive or mutating actions require an operation whose scope authorizes them.
12. **A temporary working copy is not authoritative archive storage.** Creating a local processing copy does not change archive ownership or content identity.

## Ingest and merge

13. **Ingest has direction.** Source and target archive must be explicitly identifiable.
14. **Conflict handling is policy-driven.** Code must not silently invent conflict resolution behavior.
15. **Historical metadata must not be silently discarded.** When metadata/tag assertions conflict, provenance/history must remain representable even if a derived effective value is later chosen.

## Persistence

16. **Persistent schema changes are explicit and versioned.**
17. **Unsupported schema versions fail clearly rather than being interpreted optimistically.**
18. **Database relationships preserve referential integrity.**

## Safety and determinism

19. **Read-only operations must remain read-only even on failure.**
20. **Partial work must not masquerade as successful work.**
21. **Queries with no inherent ordering must use explicit ordering when deterministic output is part of the interface.**
22. **Optimization must preserve domain semantics.** A future Rust implementation may replace a bottleneck but must not change content identity or catalog meaning.

## Scope for Plan 001

23. **Plan 001 does not manage archive storage.**
24. **Plan 001 does not resolve duplicates destructively.**
25. **Plan 001 does not implement tag policy, ingest, merge, or working-copy lifecycle.**
