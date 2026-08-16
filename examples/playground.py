"""Run a safe demonstration of the Plan 001 catalog API.

Execute with: ``uv run python examples/playground.py --keep``.
"""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from archiver import Catalog


def parse_args() -> argparse.Namespace:
    """Parse the optional workspace controls for the playground."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep an automatically created temporary workspace after the script exits",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="create the source tree and catalog in this empty or new directory",
    )
    return parser.parse_args()


def run(workspace: Path) -> None:
    """Create a source tree, scan it, and print useful query results."""
    source = workspace / "source"
    source.mkdir()
    (source / "reports").mkdir()
    (source / "reports" / "summary.txt").write_text("Quarterly summary\n", encoding="utf-8")
    (source / "copy-a.txt").write_bytes(b"same content")
    (source / "copy-b.txt").write_bytes(b"same content")

    catalog_path = workspace / "catalog.sqlite"
    with Catalog.create(catalog_path) as catalog:
        summary = catalog.scan_directory(source)

        print(f"Workspace: {workspace}")
        print(f"Catalog database: {catalog_path}")
        print("Scan summary:")
        print(summary)
        print("\nCurrent files:")
        for observation in catalog.current_files(source):
            print(f"- {observation.relative_path} ({observation.content_id.digest[:12]}...)")
        print("\nDuplicate groups:")
        for group in catalog.duplicate_groups(source):
            paths = ", ".join(str(observation.relative_path) for observation in group)
            print(f"- {paths}")


def main() -> None:
    """Run the playground and preserve the workspace when requested."""
    args = parse_args()
    if args.workspace is not None:
        args.workspace.mkdir(parents=True, exist_ok=True)
        if any(args.workspace.iterdir()):
            raise SystemExit(f"workspace must be empty: {args.workspace}")
        run(args.workspace.resolve())
    elif args.keep:
        workspace = Path(mkdtemp(prefix="archiver-playground-"))
        run(workspace)
        print("\nWorkspace was kept for inspection. Delete it manually when finished.")
    else:
        with TemporaryDirectory(prefix="archiver-playground-") as temporary_directory:
            run(Path(temporary_directory))


if __name__ == "__main__":
    main()
