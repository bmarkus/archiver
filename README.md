# Archiver

Archiver is a content-oriented local file catalog. It records content identity, observed file instances, scan history, duplicates, and provenance-aware tags without modifying source files.

The project is being developed incrementally, starting with a reliable file catalog and later expanding toward managed archives.

The implementation is Python-first. Performance-critical components may later be replaced with Rust only where profiling shows a real need.

Archiver is also an educational exercise in building a system from first principles. It is developed from the bottom up through a deliberate combination of human judgment and agent-assisted work, with particular emphasis on clear structure, sound architecture, and readable code.

The project’s incremental plans and commit history document not only the resulting features, but also the process of designing and implementing them. Development proceeds in small, reviewable steps while leaving room for new discoveries to reshape earlier decisions—even foundational ones. The evolution of the system is intended to be as instructive as the finished result.


## Project status

Phase 1 is feature-frozen. Its scope, completed capabilities, and design boundary are summarized in [Phase 1 — Catalog Foundation](docs/phases/phase-1-catalog-foundation.md).

A frozen reference is preserved on the [`phase/1-catalog-foundation`](https://github.com/bmarkus/archiver/tree/phase/1-catalog-foundation) branch. It allows readers to inspect the Phase 1 endpoint and the commit history leading to it independently of later development.

The first implementation phase established the catalog foundation: content identity, scan history and current state, filesystem reconciliation, bounded inspection, and provenance-aware content tags.

## Development phases

Incremental design and implementation history is preserved in the [plan archive](docs/plans/README.md).

## Catalog concepts

A catalog is an index of files Archiver has observed. It records their content, where copies were found, their tags, and the history of successful scans. Cataloging describes files; it does not modify or manage them.

### Content and file instances

Archiver identifies **content** by its bytes rather than its filename or path. A **file instance** is one observed copy of that content at a particular location.

For example, copying the same photograph from a laptop to an external drive creates two file instances with one content identity. Editing the photograph in place creates new content at the same path.

### Scans and history

A scan records what Archiver observed at a location. The latest successful scan defines its current state, while earlier scans remain available as history.

For example, if a file appeared in Monday’s scan but disappeared by Tuesday, the catalog retains the earlier observation while reporting that the file is no longer current. A failed scan never replaces the last successful state.

### Duplicates

Files are duplicates when their bytes are identical, even if their names or locations differ.

For example, `report.pdf` and `report-final.pdf` are duplicates if they contain the same bytes. Files that share a name but contain different bytes are not duplicates. Finding duplicates does not authorize Archiver to remove either copy.

### Tags

Tags describe content rather than a particular pathname. Applying a tag through a path first identifies the content stored there.

For example, tagging one copy of a photograph as `family` associates that tag with the photograph’s content, so another identical copy represents the same tagged content. Archiver also records whether a tag came from a user or a tool and preserves retracted assertions as history.

### Catalogs and archives

A catalog observes and describes files. An archive would additionally manage designated storage and therefore requires explicit authority to change it.

The current implementation is a catalog. It does not manage archive storage.

## Repository structure

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── docs/
│   ├── domain-model.md
│   ├── invariants.md
│   └── plans/
├── src/
│   └── archiver/
└── tests/
```

Important project documents:

* [`docs/domain-model.md`](docs/domain-model.md) — conceptual model and terminology.
* [`docs/invariants.md`](docs/invariants.md) — properties that implementations must preserve.
* [`AGENTS.md`](AGENTS.md) — instructions for coding agents working in this repository.
* [`docs/plans/README.md`](docs/plans/README.md) — chronological archive of implementation plans and decisions.

## Development

Requirements:

* Python 3.12 or later
* [`uv`](https://docs.astral.sh/uv/)

Install dependencies:

```bash
uv sync
```

## Try it

Run the safe playground example. It creates sample files and a catalog in a temporary directory, then prints the directory path so it can be inspected afterwards:

```powershell
uv run python examples/playground.py --keep
```

Open the interactive tutorials:

```powershell
uv run jupyter lab notebooks
```

### Notebook privacy and commits

Public tutorials live in `notebooks/public/` and use only temporary, synthetic data. Personal exploration belongs in `notebooks/private/`, which Git ignores. Do not move a private notebook into the public directory until its code and outputs contain no local paths, filenames, or other private data.

The private local-catalog notebook loads `ARCHIVER_NOTEBOOK_SOURCE` from the repository-root `.env` file. It creates or opens its catalog at `.archiver/catalog.sqlite` within that source directory. Create the ignored local settings file from the tracked template, then edit the value:

```powershell
Copy-Item .env.example .env
# Set ARCHIVER_NOTEBOOK_SOURCE=<directory-to-explore> in .env
uv run jupyter lab notebooks/private
```

Never commit `.env`; it is intentionally ignored. `.env.example` documents the required variable without containing a local path.

`nbstripout` removes notebook outputs and execution counts from the version stored by Git. After cloning, install the repository-local Git filter once:

```powershell
uv sync
uv run nbstripout --install --attributes .gitattributes
```

Before committing a public notebook, strip and verify it, then review the staged change:

```powershell
uv run nbstripout notebooks/public/plan-001-catalog-tour.ipynb
uv run nbstripout --verify notebooks/public/plan-001-catalog-tour.ipynb
git add .gitattributes notebooks/public/plan-001-catalog-tour.ipynb
git diff --cached
git commit -m "Update public catalog tutorial"
```

The shared `.gitattributes` records which files use the filter, but Git intentionally requires each clone to install the filter locally. See the [nbstripout documentation](https://github.com/kynan/nbstripout/blob/main/README.md) for details.

### Catalog CLI

CLI for the archiver (explore on the shell)

```powershell
uv run archiver catalog -h
usage: archiver catalog [-h] {init,migrate,info,refresh,scan,duplicates,files,tags} ...

positional arguments:
  {init,migrate,info,refresh,scan,duplicates,files,tags}
    init                create a catalog without scanning
    migrate             explicitly migrate an older catalog schema
    info                show catalog and current-scan information
    refresh             reconcile and atomically refresh the catalog root
    scan                compatibility alias for refresh
    duplicates          show aggregate duplicate metrics
    files               browse bounded current-file results
    tags                add, remove, and query content tags
```

Preview reconciliation without changing the catalog:

```powershell
uv run archiver catalog refresh C:\path\to\root --dry-run --details changes
```

Inspect duplicate groups with bounded member output:

```powershell
uv run archiver catalog duplicates C:\path\to\root --details --group-limit 20 --member-limit 20
```

Manage content-level tags through a current path or SHA-256 digest:

```powershell
uv run archiver catalog tags add C:\path\to\root favorite --path photos/example.jpg
uv run archiver catalog tags list C:\path\to\root --path photos/example.jpg
uv run archiver catalog tags find C:\path\to\root favorite --limit 20
uv run archiver catalog tags available C:\path\to\root --sort assertions --limit 20
```
The SQLite catalog is stored at `.archiver/catalog.sqlite` below the chosen root. The `.archiver` control directory is excluded from refreshes and scans.

Run tests:

```bash
uv run pytest
```

Run linting:

```bash
uv run ruff check .
```

Check formatting:

```bash
uv run ruff format --check .
```

Run static type checking:

```bash
uv run mypy src tests
```

Run the full validation set before considering an implementation task complete.

## Implementation principles

The project follows a few deliberately conservative rules:

* Prefer simple Python implementations first.
* Measure before optimizing.
* Never make destructive filesystem changes implicitly.
* Keep persistent schema changes explicit and versioned.
* Keep implementation increments small and independently testable.
* Do not implement functionality belonging to future plans prematurely.


## Working with coding agents

Coding agents should begin by reading:

1. `AGENTS.md`
2. `docs/domain-model.md`
3. `docs/invariants.md`
4. the active file under `docs/plans/`

The repository documentation is the source of truth for implementation decisions.

If an implementation plan conflicts with a documented invariant, the conflict should be surfaced rather than silently resolved by changing the architecture.

## License

Archiver is licensed under the [MIT License](LICENSE).
