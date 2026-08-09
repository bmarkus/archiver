# Plan 001 — Catalog Foundation

> Working assumption for this handoff: Plan 001 is the smallest useful catalog foundation: content hashing, local directory scanning, persistent observations in SQLite, and duplicate discovery. It deliberately excludes archive management, ingest, tags, and destructive operations.

## Goal

Implement a Python library that can create a catalog, scan a local directory without modifying it, identify regular-file content using SHA-256, persist scan observations in SQLite, expose the most recent successful state, and identify groups of duplicate content.

This plan establishes semantics and tests that later archive, ingest, tagging, and working-copy features can build on.

## Non-goals

Do not implement:

- archive-managed storage;
- source-to-target ingest;
- catalog merge;
- tags or metadata conflict policy;
- automatic deletion, moving, renaming, linking, or deduplication;
- working-copy/cache management;
- filesystem watching;
- network or cloud storage;
- content-type extraction;
- image/document metadata extraction;
- parallel hashing;
- performance-specific Rust code;
- a GUI;
- a public command-line interface, unless needed only as a tiny development aid.

## Tooling

Set up the project with:

- Python 3.12+
- `uv`
- `pytest`
- `ruff`
- `mypy`

Use a `src/` layout.

Prefer the standard library for Plan 001:

- `sqlite3`
- `hashlib`
- `pathlib`
- `dataclasses`
- standard typing facilities

Do not introduce an ORM.

## Proposed package layout

```text
src/
└── archiver/
    ├── __init__.py
    ├── catalog.py
    ├── hashing.py
    ├── models.py
    └── errors.py

tests/
├── unit/
└── integration/
```

The exact split may change if a simpler layout is clearer, but responsibilities should remain separated.

## Domain types

Introduce explicit Python types/dataclasses for at least the following concepts.

### ContentId

Represents cryptographic content identity.

Fields:

- `algorithm: str`
- `digest: str`

For Plan 001 the only supported algorithm is `sha256`.

The hexadecimal digest should use one canonical representation: lowercase hexadecimal.

### FileObservation

Represents one regular file observed during a successful scan.

Fields should include at least:

- location identifier or location object;
- relative path;
- content identity;
- size in bytes;
- observed `mtime_ns`.

The public representation must not use absolute pathname as content identity.

### ScanSummary

Return a summary from a completed scan with at least:

- number of regular files observed;
- total bytes observed;
- number of distinct content identities;
- number of duplicate-content groups.

Exact field names may differ if they remain clear and typed.

## Hashing

Provide a narrow hashing function/interface.

Required behavior:

1. Hash regular-file bytes with SHA-256.
2. Read files incrementally in chunks; do not load the entire file into memory.
3. Empty files must hash correctly.
4. Return a `ContentId`.
5. Keep hashing isolated enough that a later optimized implementation can replace it without changing callers or semantics.

Do not use filename, mtime, inode, or size as a substitute for the hash in Plan 001.

## Catalog persistence

Use SQLite.

A catalog database must persist:

- a catalog identifier;
- schema version;
- known locations;
- scan runs;
- content identities;
- file observations.

### Suggested schema semantics

The exact SQL names are implementation details, but the schema must represent these relationships.

#### Catalog metadata

Persist at least:

- a stable catalog UUID;
- schema version.

Creating a catalog initializes schema version `1`.

Opening a database with an unsupported schema version must raise a clear domain error.

#### Location

A location identifies a scan root.

Persist at least:

- stable database identifier;
- canonical root path for this initial local-filesystem implementation.

A catalog may contain more than one location.

#### Content

Persist at least:

- hash algorithm;
- digest;
- size.

`(algorithm, digest)` must identify one content record.

If existing content identity is encountered with a contradictory size, fail clearly rather than silently accepting inconsistent data.

#### Scan run

Persist at least:

- scan identifier;
- location;
- started timestamp;
- completion timestamp when successful;
- status.

Statuses should distinguish at least:

- running;
- completed;
- failed.

Only a completed scan can become the current state for a location.

#### File observation

Persist at least:

- scan;
- relative path;
- content record;
- size;
- `mtime_ns`.

A relative path must be unique within one scan.

Store paths in one canonical textual representation. Prefer POSIX-style relative separators in the database so catalog output is deterministic across Python path objects.

Enable SQLite foreign-key enforcement.

## Scanning behavior

Provide a library operation equivalent to:

```python
catalog.scan_directory(root: Path) -> ScanSummary
```

The exact API may differ, but behavior must match the following.

### Traversal

- Recursively scan `root`.
- Include regular files.
- Do not follow symbolic links.
- Ignore symbolic-link entries rather than treating their targets as scanned files.
- Use deterministic ordering when exposing results.
- Record paths relative to `root`.

### Source safety

Scanning is read-only.

It must not:

- write file contents;
- rename files;
- delete files;
- change permissions;
- change timestamps intentionally;
- create sidecar files inside the scanned tree.

The catalog database itself may live outside or inside the tree. If it lives inside the scanned root, ensure the catalog database and its SQLite sidecar files are not accidentally cataloged as source content. Prefer documenting that the database should normally live outside the root and implement a safe exclusion when the database is inside it.

### Successful scan

When all required files are processed successfully:

1. mark the scan completed;
2. make it the current scan for that location;
3. return `ScanSummary`.

The current file state for a location is the file observations belonging to its most recent completed scan.

This means a file removed between two successful scans disappears from the current view without deleting historical observations.

### Failed scan

If hashing or persistence fails partway through a scan:

- the scan must not become current;
- the previous successful scan, if any, remains the current state;
- partial observations from the failed scan may be retained for diagnostics or discarded, but they must never appear in the current-file query;
- the scan should be recorded as failed when practical.

A stale `running` scan left by process termination must likewise never be treated as current.

Do not build elaborate crash-recovery machinery in Plan 001.

## Catalog queries

Implement typed query methods sufficient for tests and later plans.

Provide the semantic equivalent of:

### Current files

Return all current `FileObservation` records for a location, ordered deterministically by relative path.

### Lookup by content identity

Return current file observations matching a supplied `ContentId`.

### Duplicate groups

Return groups of current file observations where two or more paths share one content identity.

A duplicate group is based on content hash, regardless of filename.

Do not resolve or delete duplicates.

## Errors

Define a small set of domain-specific exceptions rather than leaking every raw SQLite/filesystem exception through the public API.

At minimum, distinguish:

- invalid/unsupported catalog database;
- scan failure.

Do not build a large exception hierarchy.

## Tests

### Unit tests

Test at least:

1. SHA-256 of known bytes produces the expected lowercase digest.
2. Empty file hashing is correct.
3. Hashing operates on bytes, not pathname/metadata.
4. `ContentId` equality behaves as expected.
5. Path normalization used for persistence is deterministic.

### Integration tests

Use `tmp_path` or equivalent temporary directories.

Test at least:

1. **Create/open**
   - create a catalog;
   - close it;
   - reopen it;
   - catalog UUID and schema version persist.

2. **Single-file scan**
   - scan one regular file;
   - current view contains exactly that file;
   - size, relative path, and hash are correct.

3. **Nested directories**
   - nested regular files are discovered;
   - stored paths are relative and deterministic.

4. **Duplicate content**
   - create two files with different names and identical bytes;
   - they produce one content identity;
   - duplicate query returns one group containing both paths.

5. **Same name, different bytes**
   - equivalent filenames in separate locations do not imply duplicate content.

6. **Rename across scans**
   - scan;
   - rename a file without changing bytes;
   - scan again;
   - current path changes;
   - content identity remains the same.

7. **Deletion across scans**
   - scan two files;
   - remove one;
   - scan again;
   - current view contains only the remaining file;
   - historical scan data may still contain the earlier observation.

8. **Content change across scans**
   - scan a path;
   - replace its bytes;
   - scan again;
   - same relative path now points to a different content identity.

9. **Symlink behavior**
   - create a symlink where supported;
   - scanning does not follow/catalog its target through the symlink.

10. **Failed scan does not replace current state**
    - complete an initial scan;
    - force a controlled hashing failure during a second scan;
    - verify the first scan remains current.

11. **Source remains unchanged**
    - compare source file bytes before and after scan;
    - verify contents are unchanged.

12. **Database inside scan root**
    - if supported by the implementation, place the catalog DB within the scanned root;
    - verify the DB and SQLite sidecars are excluded from cataloged source files.

Tests that depend on OS features such as symlinks may skip cleanly when unsupported.

## Acceptance criteria

Plan 001 is complete only when all of the following are true:

- [ ] Repository uses a `src/` layout and has a valid `pyproject.toml`.
- [ ] `uv run pytest` passes.
- [ ] `uv run ruff check .` passes.
- [ ] `uv run ruff format --check .` passes.
- [ ] `uv run mypy src tests` passes.
- [ ] A catalog can be created and reopened.
- [ ] Schema versioning exists and unsupported versions fail clearly.
- [ ] A local directory can be scanned recursively.
- [ ] Regular-file bytes are identified with SHA-256.
- [ ] Hashing is streaming rather than whole-file loading.
- [ ] Symlinks are not followed.
- [ ] The source tree is not modified by scanning.
- [ ] Multiple paths can refer to the same content record.
- [ ] Duplicate groups are discoverable by content identity.
- [ ] The latest successful scan defines current state.
- [ ] A failed scan cannot replace the current successful state.
- [ ] Renames preserve content identity when bytes are unchanged.
- [ ] Removed files disappear from the current view after a later successful scan.
- [ ] No archive ingest, tag policy, destructive deduplication, working-copy system, or Rust code is introduced.

## Codex implementation prompt

After these repository files are committed, a suitable first Codex prompt is:

> Read `AGENTS.md`, `docs/domain-model.md`, `docs/invariants.md`, and `docs/plans/001-catalog-foundation.md`. Implement Plan 001 only. Keep the implementation simple and Python-first. Run every required test/check. If the plan conflicts with a documented invariant, do not silently reinterpret the architecture; report the conflict. When finished, summarize files changed, tests added, schema choices, and any deviations from the plan.

## Exit condition

Stop after Plan 001 passes its acceptance criteria.

Do not start archive ingest, tags, merge logic, or Plan 002 automatically.
