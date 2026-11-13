"""Compare temporary catalogs without copying files or changing their inventories.

Run from the repository with: uv run python examples/compare_content.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from archiver import Catalog


def main() -> None:
    with TemporaryDirectory(prefix="archiver-compare-") as directory:
        base = Path(directory)
        source_root, target_root = base / "source", base / "target"
        source_root.mkdir()
        target_root.mkdir()
        (source_root / "shared.txt").write_bytes(b"shared")
        (source_root / "missing.txt").write_bytes(b"missing")
        (target_root / "renamed.txt").write_bytes(b"shared")
        with Catalog.create(base / "source.sqlite") as source, Catalog.create(base / "target.sqlite") as target:
            source.scan_directory(source_root)
            target.scan_directory(target_root)
            with source.compare_content(target, source_root=source_root, target_root=target_root) as comparison:
                summary = comparison.summary()
                assert summary.source_content_count == 2
                assert summary.shared_content_count == summary.missing_source_content_count == 1
                print(summary)
                for batch in comparison.batches(batch_size=1, include_present=True):
                    for row in batch:
                        print(row.source_relative_path, "represented" if row.in_target else "missing")
            # Comparison has closed; both caller-owned Catalogs are still usable.
            assert source.files(source_root).count() == 2
            assert target.files(target_root).count() == 1


if __name__ == "__main__":
    main()
