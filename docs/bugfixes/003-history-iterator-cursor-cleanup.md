# Bugfix 003 — History iterator cursor cleanup

**Status:** Implemented (reviewed).

**Review checkpoint:** 2026-11-08 — Reviewed at the user's request for the
catalog-to-archive handoff. Earlier dated implementation/review notes and
checklists remain historical; this checkpoint supersedes earlier pending
statuses and does not activate follow-on implementation. See the
[handoff record](../plans/README.md#catalog-review-and-archive-handoff--2026-11-08).

## Reproduction

Partially consume `Catalog.scan_history()` or `Catalog.observation_history()`,
then explicitly close the generator. The generator-owned SQLite cursor is not
closed in a `finally` block. A row-materialization exception has the same gap.

## Root cause

The history generators iterate a cursor directly without explicit cursor
ownership and cleanup.

## Affected invariants

Read-only safety (19), partial-work integrity (20), and deterministic query
lifecycle behavior (21).

## Proposed solution

Move retained history read execution behind focused private query functions.
Each generator opens its cursor on first advancement and closes it in `finally`
on exhaustion, explicit close, or error. Keep the existing lazy public iterator
API and ordering.

## Non-goals

No eager history collection, history handles, snapshots, schema changes, or
changes to mutation transactions.

## Acceptance tests

- Execution begins on first advancement, not generator creation.
- Exhaustion, explicit partial close, and materialization failure close only
  the owned cursor.
- Interleaved history and query cursors remain independent.
- Existing result shapes, ordering, errors, and close/reopen persistence remain.

## Outcome

History iterators now open on first advancement and close their owned cursor in
`finally` on exhaustion, explicit close, and materialization failure. The
retained-read and full validation gates passed with no schema/source mutation.
