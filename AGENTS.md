# AGENTS.md

# Archiver

Archiver is a content-oriented file cataloging and archival system.

The current implementation is intentionally Python-first. Do not introduce Rust until profiling demonstrates a real bottleneck and a later plan explicitly authorizes that work.

## Read before changing code

Read these files before implementation or architectural changes:

1. `docs/domain-model.md`
2. `docs/invariants.md`
3. The active plan under `docs/plans/`
4. The applicable plan under `docs/bugfixes/` when correcting a behavior bug.

The active implementation plan is:

- `docs/plans/003.3c-tag-vocabulary-cli.md`

`docs/plans/003.3-bounded-tag-and-file-search.md` is the umbrella design
record for Plans 003.3a-003.3c, not a single implementation increment.

The domain model and invariants are authoritative. If an implementation plan appears to conflict with them, stop changing the design and report the conflict.

## Bug-fix plans

Before changing behavior to correct a bug, add a concise plan in `docs/bugfixes/`.
Each plan must record the reproduction, root cause, affected invariants, proposed
solution, non-goals, and acceptance tests. Update its outcome after validation.

## Scope discipline

Implement only the active plan.

Do not add archive ingest, merge policies, tag management, working-copy management, remote storage, GUI features, background watchers, or Rust code unless the active plan explicitly requires them.

Prefer the smallest design that satisfies the current plan while preserving documented invariants.

## Python development

Use:

- Python 3.12+
- `uv` for environments and dependency management
- `pytest` for tests
- `ruff` for linting and formatting
- `mypy` for static type checking

Prefer the Python standard library unless an external dependency provides clear value.

For the initial catalog database, prefer `sqlite3` directly rather than introducing an ORM.

Use type annotations for public interfaces and meaningful internal boundaries.

## Design rules

- Separate content identity from filesystem pathname.
- Do not treat a path as the identity of a file's contents.
- Keep filesystem access behind narrow functions/classes so later optimization is possible.
- Keep hashing behind a narrow interface so a future Rust implementation can replace it without changing domain semantics.
- Do not optimize based on assumptions. Measure first.
- Do not silently change a persistent database schema.
- Do not weaken an invariant merely to make tests pass.
- Avoid global mutable state.
- Make ordering deterministic where practical.
- Prefer explicit domain types over passing unrelated values as raw strings everywhere.

## Filesystem safety

Unless an active plan explicitly authorizes mutation:

- never modify source files;
- never rename, move, delete, or rewrite source files;
- never follow symbolic links during recursive scans;
- treat filesystem metadata such as path and mtime as observations, not content identity.

Tests must use temporary directories and must never depend on the user's real archive or home directory.

## Database safety

- Enable SQLite foreign-key enforcement.
- Use transactions for state changes that must remain internally consistent.
- A failed or interrupted scan must not replace the most recent successful catalog state.
- Persist enough schema-version information to reject unsupported database versions cleanly.

## Tests

Every behavior in an active plan needs an automated test where practical.

Tests should include:

- normal behavior;
- duplicate content;
- empty files where relevant;
- nested paths;
- failure/interruption behavior;
- persistence across close/reopen;
- assertions that source files are unchanged for read-only operations.

Avoid tests whose result depends on directory enumeration order, wall-clock timing, external network access, or machine-specific paths.

## Completion checklist

Before declaring a plan complete:

1. Run `uv run pytest`.
2. Run `uv run ruff check .`.
3. Run `uv run ruff format --check .`.
4. Run `uv run mypy src tests`.
5. Compare the implementation against every acceptance criterion in the active plan.
6. Summarize:
   - files changed;
   - tests added;
   - user-visible behavior added;
   - schema changes;
   - architectural decisions made;
   - unresolved questions or deviations.

Do not begin the next plan automatically.
