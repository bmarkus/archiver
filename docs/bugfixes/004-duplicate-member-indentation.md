# Bugfix 004 — Duplicate member indentation

**Status:** Implemented (reviewed).

**Review checkpoint:** 2026-11-08 — Reviewed at the user's request for the
catalog-to-archive handoff. Earlier dated implementation/review notes and
checklists remain historical; this checkpoint supersedes earlier pending
statuses and does not activate follow-on implementation. See the
[handoff record](../plans/README.md#catalog-review-and-archive-handoff--2026-11-08).

## Reproduction

Run `archiver catalog duplicates ROOT --details` on a temporary root containing
duplicate files. Member paths have only the existing seven-space indentation,
which does not visually separate them enough from the group status row.

## Root cause

`_print_duplicate_group` uses a fixed space-only prefix for member rows.

## Affected invariants

Preserve deterministic presentation (21) and read-only source safety (19).
The user explicitly requests this narrow output-format change after Plan 004.9;
all other CLI/API behavior remains as implemented.

## Proposed solution

Prepend one literal tab to the existing seven-space member prefix for each
selected duplicate group, including paged details. Keep the group status row,
ordering, summaries, numbering, bounds, and path text unchanged. Document the
member indentation in the CLI reference.

## Non-goals

No query, schema, dependency, pagination, path-selection, or source-file changes.
No new command syntax or general renderer changes.

## Acceptance tests

Update existing end-to-end CLI assertions to require a tab before the existing
member indentation for both ordinary and paged detail output. Retain group row,
member-bound, ordering, and summary assertions. Run pytest, Ruff lint/format,
mypy, and whitespace checks.

## Outcome

Member rows now start with one literal tab followed by their original seven
spaces. Group rows, page ordinals, summaries, bounds, and path text are unchanged.
Updated the existing ordinary/paged CLI assertions and the CLI reference; no
new test cases, schema changes, dependencies, or architectural changes.

Validation: `uv run pytest` (238 passed, 3 existing platform-dependent skips),
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`,
and `git diff --check` passed. New-plan whitespace inspection passed. All
acceptance criteria are satisfied; no unresolved questions or deviations.
