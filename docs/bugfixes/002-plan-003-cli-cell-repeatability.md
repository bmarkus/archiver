# Bug fix 002 — Plan 003 CLI cell repeatability

**Status:** Implemented (reviewed).

**Review checkpoint:** 2026-11-08 — Reviewed at the user's request for the
catalog-to-archive handoff. Earlier dated implementation/review notes and
checklists remain historical; this checkpoint supersedes earlier pending
statuses and does not activate follow-on implementation. See the
[handoff record](../plans/README.md#catalog-review-and-archive-handoff--2026-11-08).

## Summary and reproduction

Run the “Equivalent CLI use” cell in `notebooks/public/plan-003-bounded-duplicate-inspection.ipynb` twice without recreating its temporary source root. The second run raises `subprocess.CalledProcessError` when `archiver catalog init` exits with status 1.

## Root cause

The cell unconditionally invokes `catalog init`. The first run correctly creates `source/.archiver/catalog.sqlite`; the CLI deliberately refuses to overwrite that existing catalog on the second run.

## Invariants

The CLI must not silently overwrite a catalog. The notebook must use only its synthetic temporary source and keep duplicate inspection observational.

## Proposed solution

Initialize only when the synthetic root lacks `.archiver/catalog.sqlite`; then run refresh and duplicate inspection on every cell execution.

## Non-goals

This does not change CLI initialization behavior, overwrite policy, catalog schema, or duplicate-inspection semantics.

## Acceptance tests

- Running the extracted CLI cell twice completes successfully.
- The second execution reuses the existing synthetic catalog.
- The duplicate inspection output remains bounded and observational.

## Outcome

The CLI cell now checks for the synthetic root's .archiver/catalog.sqlite before running catalog init. It always refreshes and inspects the existing catalog, so a repeated cell execution completes without overwriting the catalog.

Validation: extracted notebook code was run with the CLI cell executed twice consecutively; both executions completed and produced the bounded duplicate report.
