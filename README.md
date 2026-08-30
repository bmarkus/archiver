# Archiver

Archiver is a content-oriented file cataloging and archival system.

The project is being developed incrementally, starting with a reliable file catalog and later expanding toward managed archives, ingest, tagging, metadata reconciliation, duplicate handling, and local working copies for expensive processing.

The implementation is Python-first. Performance-critical components may later be replaced with Rust only where profiling shows a real need.

Archiver is also an educational exercise in building a system from first principles. It is developed from the bottom up through a deliberate combination of human judgment and agent-assisted work, with particular emphasis on clear structure, sound architecture, and readable code.
The project’s incremental plans and commit history document not only the resulting features, but also the process of designing and implementing them. Development proceeds in small, reviewable steps while leaving room for new discoveries to reshape earlier decisions—even foundational ones. The evolution of the system is intended to be as instructive as the finished result.

## Project status

Early development.

The current implementation target is:

**Plan 003.3b — Tag-Aware Current-File Search**

This establishes the core catalog model:

* persistent SQLite catalog;
* SHA-256 content identity;
* recursive local-directory scanning;
* scan history;
* current-state queries;
* duplicate-content discovery;
* safe handling of failed scans;
* filesystem reconciliation and atomic refresh application;
* bounded file and duplicate inspection;
* provenance-aware content tags with explicit schema migration;
* bounded multi-tag content and tag-aware current-file search.

Archive ingest, automatic tag classification, tag merge policy, destructive duplicate handling, and managed archive storage remain out of scope.

## Core ideas

### Content identity

Files are identified by their contents, not by their pathname.

Two files with different names or locations may represent the same content if their cryptographic hashes are equal.

Conversely, the same pathname may contain different content at different times.

### Catalogs and archives

A **catalog** describes files and their metadata.

An **archive** is conceptually a catalog associated with storage that Archiver is explicitly authorized to manage.

The two do not require fundamentally different database models.

### Scans are observations

Scanning a directory is read-only.

A scan records what was observed at a particular location. The latest successful scan defines the current catalog state for that location.

A failed or interrupted scan must never replace the previous successful state.

### History and provenance

The design favors preserving historical information rather than silently overwriting it.

Tag assertions now retain user/system provenance, producer name, producer version, and retraction history. Catalog merge policy and public historical-tag queries remain future work.

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
│       ├── 001-catalog-foundation.md
│       └── 002-catalog-refresh-and-reconciliation.md
├── src/
│   └── archiver/
└── tests/
```

Important project documents:

* [`docs/domain-model.md`](docs/domain-model.md) — conceptual model and terminology.
* [`docs/invariants.md`](docs/invariants.md) — properties that implementations must preserve.
* [`docs/plans/002-catalog-refresh-and-reconciliation.md`](docs/plans/002-catalog-refresh-and-reconciliation.md) — current implementation plan and refresh design.
* [`AGENTS.md`](AGENTS.md) — instructions for coding agents working in this repository.

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

## Development principles

The project follows a few deliberately conservative rules:

* Prefer simple Python implementations first.
* Measure before optimizing.
* Keep content identity independent of path and storage location.
* Never make destructive filesystem changes implicitly.
* Keep persistent schema changes explicit and versioned.
* Preserve historical information where future reconciliation may depend on it.
* Keep implementation increments small and independently testable.
* Do not implement functionality belonging to future plans prematurely.

## Current roadmap

The roadmap is intentionally incremental.

### Plan 001 — Catalog foundation

Create and open catalogs, scan local directories, hash file content, persist observations, query current state, and detect duplicate content.

### Later plans

Likely later stages include:

* richer catalog queries;
* richer tag and metadata history and merge policy;
* catalog comparison and merge;
* archive-managed storage;
* source-to-archive ingest;
* duplicate-resolution policies;
* local working-copy/cache support for expensive processing;
* performance profiling and targeted optimization.

These later stages are not yet part of the current implementation contract.

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
