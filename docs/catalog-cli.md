# Catalog CLI reference

Run `uv run archiver catalog --help` for commands and append `--help` to any
command for its options. A root’s database is `.archiver/catalog.sqlite`;
refreshes exclude the `.archiver` control directory. Scanning and inspection
leave source files unchanged. Catalog mutations require explicit commands.

## Query commands

| CLI command | Public API used | Default display bounds |
| --- | --- | --- |
| `catalog files ROOT` | `files(root)` with separate count/size reductions | 20 files |
| `catalog files ROOT --show-tags` or tag options | File predicates and `with_tags()`; separate file/content/size reductions | 20 files, 3 active tag names per content |
| `catalog duplicates ROOT` | `duplicates(root).summary()` after exact current-scan check | Summary only |
| `catalog duplicates ROOT --details` | Bounded duplicate group `stream()` plus complete summary | 20 group metric rows; no filenames |
| `catalog duplicates ROOT --details --member-limit N` (N > 0) | Duplicate query `fetch()` plus complete summary | 20 groups, at most N member paths per group |
| `catalog tags find ROOT TAG [TAG ...]` | `contents()` predicates and `with_details(root=...)` | 20 content identities, 3 active tag names each |
| `catalog tags find ROOT TAG --details` | Same content detail query, displaying its path previews | Also 20 paths per content identity |
| `catalog tags available ROOT` | `tags()` vocabulary query and complete count | 20 active tag names |

The CLI intentionally exposes bounded inspection, while the Python API also
supports explicit file and duplicate streams. Counts, logical file sizes,
current path totals, and duplicate summaries cover the complete matching set,
regardless of display bounds. Separate totals and rows are separate terminals;
a command does not promise one atomic snapshot if another writer changes state.

File and duplicate queries describe the root’s latest successful scan. Content
and active tag vocabulary are catalog-wide: tagged content remains searchable
when its current paths disappear. The root for `tags find` scopes only current
path counts/previews. A catalog with no successful scan returns empty file rows;
duplicate output says `Current scan: none`. Scanned empty/duplicate-free roots
report zero duplicate totals. Empty content/vocabulary searches report zero matches.

## Filtering, sorting, and display

Files accept `--path GLOB`, matched against the complete POSIX relative path
using case-sensitive SQLite GLOB syntax. Repeat `--tag TAG` for AND matching;
`--match-any-tag` selects OR across those requested tags. `--provenance user|system`
requires at least one requested tag. `--show-tags` displays tags without filtering.
Displayed tags include all active provenance, independently of matching provenance.

File `--sort path` defaults to ascending order; `--sort size` and `--sort date`
default to descending. `--reverse` flips only the selected primary ordering.
Size/date ties always use ascending relative paths, including under reverse.
`--limit N` changes the file row bound.

Duplicate details order groups by complete potential redundant bytes descending,
then full content algorithm/digest ascending. Members use relative path ascending.
`--details` defaults to a table of group metrics without filenames. The default
`--member-limit 0` also suppresses the PATHS preview-count column and avoids
fetching member rows. Set `--member-limit N` to a positive value to include at
most N filenames per group; `--member-limit 20` restores the earlier CLI preview.
`--group-limit N` remains positive and defaults to 20 groups. The Python API
still defaults to 20 members and rejects `.member_limit(0)`; this CLI-only mode
uses bounded group handles rather than passing zero to that API. Member
paths appear on separate lines with an additional leading tab before their
existing indentation, visually nesting them below each group status row. Empty
files can form duplicate groups with zero redundant bytes. Summary-only output
does not fetch member rows. There is no CLI group-sort or unbounded stream flag.

Content search accepts `--match all|any` (default `all`) and optional
`--provenance user|system`. Content rows use full algorithm/digest ascending;
`--limit N` bounds rows and `--path-limit N` bounds displayed paths per row.
Repeated requested names are deduplicated for matching and displayed in request
order. Content identity and path counts are different totals.

Vocabulary accepts `--regex REGEX`, with Python `re.search` semantics, and
`--provenance user|system` to scope both inclusion and usage counts. Name sorting
is ascending by default; `--sort content|assertions` sorts usage descending.
`--reverse` flips the primary direction; usage ties remain name ascending.
`--limit N` bounds displayed names.

Tagged file/content previews accept `--display-tag-limit N` or `--all-tags`,
mutually exclusively. All-tags display wraps tag names and retains bounded
parent rows. Group/row/tag limits must be positive integers; the CLI duplicate member limit
accepts nonnegative integers. Default previews show
an overflow count when tags are omitted. Tables abbreviate SHA-256 digests for
display; exact `tags add|remove|list --content SHA256` requires the full lowercase
64-character digest. A displayed prefix is never an identity selector.

Tables use the detected terminal width, with a 120-column fallback. Narrow
terminals retain minimum column widths, so some rows can exceed the terminal;
paths and bounded previews may be truncated. All-tags mode wraps tag text.

## Duplicate pages

`--group-offset N` skips N duplicate groups before `--group-limit` is applied.
The default is 0; offsets are zero-based and displayed GROUP numbers are
one-based. The group limit defaults to 20. The CLI member limit defaults to 0
(a metrics-only table); positive member limits add bounded filename previews.

```powershell
uv run archiver catalog duplicates ROOT --details --group-offset 10 --group-limit 10
uv run archiver catalog duplicates ROOT --details --group-offset 90 --group-limit 10
```

For 100 groups these display groups 11–20 and 91–100. A nonzero-offset footer
reads `Showing groups 11-20 of 100 duplicate groups.` and the GROUP column starts
at 11. Partial pages use the actual returned count. An empty nonzero-offset page
retains complete totals and says `No duplicate groups in the requested page
(offset N).` It never invents a range or claims the entire population is empty.
Omitted or explicit zero offsets preserve previous numbering and output.
No-scan output remains `Current scan: none`.

Offsets must be integers from 0 through `9223372036854775807`; malformed,
negative, or overflow values fail with argparse exit 2 before catalog opening.
Summary-only commands accept/validate `--group-offset` but do not page the
complete summary, just as group/member limits affect only details. Member paths
always start at the first member of each selected group; no member offset exists.

Each command selects live current state. Successful refreshes between page
requests can shift group positions, repeating or skipping groups. Started Python
streams and retained group handles remain pinned, but CLI summary and fetch are
separate live terminals: the printed total and page may reflect different scans
during concurrent refresh. Range numbers derive from offset and returned rows;
no shared snapshot, lock, or page session is promised. Avoid successful refreshes
while reviewing a fixed set of page positions. Full content identity, rather
than row number or abbreviated digest, identifies a group. See the
[pagination tutorial](../notebooks/public/plan-004.9-duplicate-pagination.ipynb).

## Lifecycle, exact reads, and errors

`catalog init ROOT` creates a catalog without scanning. `catalog migrate ROOT`
explicitly upgrades an older supported schema. `catalog info ROOT` uses metadata
and exact current-state reads. `catalog refresh ROOT` reconciles and atomically
applies observations; `catalog scan ROOT` remains its compatibility alias.
`--dry-run` previews changes without writing catalog state. Refresh details
accept `--details changes|all`; `--no-progress` suppresses interactive progress.

Both commands accept `--verify-hashes` to read and SHA-256 hash every included
regular file. Without it, matching relative path, size, and nanosecond mtime
reuse the cached identity; a same-size rewrite with restored mtime can be missed.
Verification also works with `--dry-run`, which remains write-free.
`--details all` labels unchanged verified files `(hash rechecked)`. Progress
continues counting completed files and observed sizes, not bytes physically read.
Read failures return 1 and preserve the previous successful catalog state.
Verification observes the files during reconciliation; application does not
reread them or guarantee they remain unchanged afterward.

Refresh detail rows follow deterministic depth-first path-component order
(`bundle/file.txt` before `bundle.zip`). This differs from the complete
relative-path string ordering used by file queries.

`catalog tags add|remove|list ROOT` takes exactly one of `--path POSIX_PATH` and
`--content SHA256`. Add/remove also require a tag name. The CLI creates user
assertions; removal retracts user assertions while retaining system assertions
and assertion history. Exact lookups preserve missing-path/content errors.

Successful commands return exit code 0. Malformed syntax, limits, tags, regexes,
and incompatible options fail before catalog opening with argparse exit code 2.
Invalid regexes are rejected before root resolution. Missing/invalid catalogs,
filesystem failures, or unresolved exact targets report `archiver: ...` on stderr
and return 1. Noninteractive reads do not emit progress.

## Examples

These commands operate on an existing root; initialize it and explicitly refresh
before inspecting its current observations:

```powershell
uv run archiver catalog init C:\path\to\root
uv run archiver catalog refresh C:\path\to\root --no-progress
uv run archiver catalog refresh C:\path\to\root --verify-hashes --dry-run --details all
uv run archiver catalog files C:\path\to\root --path "*.txt" --sort size --limit 10
uv run archiver catalog duplicates C:\path\to\root --details --group-limit 5 --member-limit 3
uv run archiver catalog tags add C:\path\to\root favorite --path photos/example.jpg
uv run archiver catalog files C:\path\to\root --tag favorite --all-tags --limit 10
uv run archiver catalog tags find C:\path\to\root favorite --details --path-limit 3
uv run archiver catalog tags available C:\path\to\root --regex "^fav" --sort assertions
```

For runnable disposable examples, see the
[004.6 public tutorial](../notebooks/public/plan-004.6-existing-read-api-migration.ipynb)
and [refresh tutorial](../notebooks/public/catalog-refresh-and-reconciliation.ipynb).
For library methods and migration, see the [Catalog API reference](catalog-api.md)
and [query grammar](query-grammar.md).

## Scan history and explicit pruning

```powershell
uv run archiver catalog history C:\path\to\root --limit 20
uv run archiver catalog prune-history C:\path\to\root --keep-last 3
uv run archiver catalog prune-history C:\path\to\root --keep-last 3 --include-failed
uv run archiver catalog prune-history C:\path\to\root --keep-last 3 --include-failed --apply
```

`history` lists newest scan IDs first, default 20 rows. It shows status, a `*`
current marker, UTC start/completion timestamps, persisted file count, and total
physical bytes. Empty scans have zero totals; absent completion time is `-`.
Failed/running totals are labeled `partial`. Normal refresh failures roll back
and do not create failed rows, but existing failed history is supported.
The API equivalent is `catalog.scan_summaries(root)`.

`prune-history` previews by default and writes nothing until `--apply` is supplied.
Default retention is the newest 3 successful scans; `--keep-last` is at least 1.
Reports include successful scans retained, successful/failed scans selected, and
observations selected. Applied output is printed after commit. Current inventory,
all content identities and tags, and other locations remain unchanged.

`--include-failed` removes only failures whose scan IDs precede the oldest
retained successful scan, and prints that cutoff. Newer failures and all running
scans remain; no successful scans means no cutoff or failed cleanup. For example,
with successful IDs 2, 4, 7 and `--keep-last 2`, delete successful ID 2; opt-in
cleanup also removes failed IDs below 4. There is no general `--force` bypass.

Both limits accept integers in `1..2**63-1`; invalid arguments return argparse
exit code 2 before opening the catalog. Missing/invalid catalogs and operational
failures return 1. Both commands require an existing catalog and read source
metadata only to resolve the root; they never scan/hash source contents.
Unknown/unscanned roots report empty history or zero pruning candidates.

No automatic pruning, vacuuming, or file-size reduction is implied. SQLite can
reuse freed pages; optional `VACUUM` is a separate maintenance decision. Finish
or close readers before applying retention, including readers in other processes.
The API and [stream lifecycle](query-grammar.md#history-retention-and-stream-lifetime)
documentation explain how pruning invalidates handles for deleted scans.
