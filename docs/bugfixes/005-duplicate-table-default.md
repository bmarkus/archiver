# Bugfix 005 — Duplicate group table by default

**Status:** Implemented (reviewed).

**Review checkpoint:** 2026-11-08 — Reviewed at the user's request for the
catalog-to-archive handoff. Earlier dated implementation/review notes and
checklists remain historical; this checkpoint supersedes earlier pending
statuses and does not activate follow-on implementation. See the
[handoff record](../plans/README.md#catalog-review-and-archive-handoff--2026-11-08).

## Reproduction and root cause

`archiver catalog duplicates ROOT --details` includes up to 20 filenames per
group. `--member-limit 0` is rejected because CLI validation mirrors the API's
positive member-preview bound. This makes a concise group-metrics table
unavailable. The user selected a group table by default with explicit positive
`--member-limit N` to add paths.

## Affected invariants

Preserve complete content-group metrics, byte identity (1), source read-only
safety (19), and deterministic ordering (21). This explicitly supersedes only
the earlier CLI filename-preview default; the public query API is unchanged.

## Proposed solution

Default CLI `--member-limit` to zero; accept nonnegative integers, rejecting
negative/malformed values before catalog open. For zero, consume the existing
bounded group stream into typed handle rows and print metrics without the PATHS
preview column or filenames. Do not fetch member rows. Positive values retain
existing bounded fetches, PATHS counts, and tab-indented filenames. Summary-only
output, group limit/offset, numbering, and totals stay unchanged. `--group-limit 0`
remains invalid. The Python API's member limits remain positive, default 20.

## Non-goals

No new API, query/result wrapper, schema, dependencies, unbounded collection,
source changes, or archive policy. No general renderer framework.

## Acceptance tests and documentation

Verify omitted/explicit zero agree on a metrics-only bounded group table, no
member SQL executes, page numbering/totals remain complete, and explicit positive
values retain their existing filename preview and member bound. Test invalid
member values and still-invalid zero group limit before catalog opening, empty/
no-scan states, read safety, and help/defaults. Preserve the explicit-preview
semantic coverage in existing tests. Update CLI docs and README; document that
only the CLI default changed. Run pytest, Ruff lint/format, mypy, and diff checks.

## Outcome

Implemented the CLI-only zero/default member mode using bounded group handles.
The group table contains identity, complete copy count, size and redundant bytes;
positive member limits retain PATHS counts and tab-indented filenames. No member
queries or preview fetches execute for table-only output. Pagination and summaries
keep their prior semantics; Python API bounds/defaults remain unchanged.

Files changed: CLI parser/execution/renderers, CLI and API usage references,
README, shared contract amendment, this plan, and CLI tests. Added five cases in
`tests/integration/test_duplicate_table_cli.py` for default/explicit-zero agreement,
bounds/numbering, no member SQL, complete metrics, positive previews, read-only
source/database state, invalid options, and help. Updated existing tests to
request positive member limits where filename previews are the intended coverage.

Validation: `uv run pytest` (243 passed, 3 existing platform-dependent skips),
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`,
`git diff --check`, and new-file whitespace/local Markdown link inspection passed.
All acceptance criteria are satisfied. No schema/dependency changes, new API,
architectural framework, unresolved questions, or deviations.
