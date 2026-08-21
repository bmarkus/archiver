# Bug fix 001 — Windows executable hashing

**Status:** Fixed

## Summary and reproduction

Scanning a stable `.exe` in a Windows directory can fail with
`RefreshFailure: reconciliation failed`. The wrapped cause is
`FileChangedDuringHashingError: file changed before hashing`.

For `7z2201-x64.exe`, the pathname and opened-handle metadata had equal device,
inode, size, and mtime values. Their `st_mode` values differed only by execute
permission bits: `Path.stat()` reported them for the `.exe` pathname while
`fstat()` did not for the opened handle.

## Root cause

`_stat_signature()` compared the complete `st_mode` value. On Windows, pathname
and handle permission bits are not a stable identity signal, producing a false
concurrent-change detection.

## Invariants

Catalog scans remain read-only and must reject a detectable replacement or
mutation rather than record an inconsistent observation. Both pathname and file
handle must still be regular files.

## Proposed solution

Compare only `st_mode` file-type bits in the stability signature. Keep the
explicit `S_ISREG()` checks and retain device, inode, size, and `mtime_ns`
comparisons.

## Non-goals

This does not add best-effort scanning, file exclusions, retries, or weaken
atomic refresh behavior.

## Acceptance tests

- Metadata that differs only in permission bits has the same stability signature.
- On Windows, `hash_file_stably()` successfully hashes a stable `.exe` file.
- A changed size or mtime continues to raise `FileChangedDuringHashingError`.

## Outcome

Implemented without changing scan atomicity. The previously failing `7z2201-x64.exe` now hashes successfully.

Validation:

- `uv run pytest` — 51 passed, 1 skipped.
- `uv run ruff check .` — passed.
- `uv run ruff format --check .` — passed.
- `uv run mypy src tests` — passed.

Review approved; this fix is ready to commit.
