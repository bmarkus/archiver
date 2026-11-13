"""Storage-only Plan 005.3 example; all files are temporary, no sync execution."""

from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from archiver import Catalog, ContentId, ManifestEntry, OperationSpec, SyncWorkspace


def main() -> None:
    with TemporaryDirectory(prefix="archiver-workspace-example-") as temporary:
        base = Path(temporary)
        source_root, target_root = base / "source", base / "target"
        source_root.mkdir()
        target_root.mkdir()
        with Catalog.create(base / "source.sqlite") as source, Catalog.create(base / "target.sqlite") as target:
            spec = OperationSpec.sync(source.operation_scope(source_root), target.operation_scope(target_root))
            with SyncWorkspace.create(base, spec, expected_count=1, expected_bytes=0) as workspace:
                # A synthetic empty-file candidate: nothing is created at either root.
                workspace.add(
                    (
                        ManifestEntry(
                            ContentId("sha256", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
                            0,
                            PurePosixPath("nested/empty"),
                            0,
                            PurePosixPath("nested/empty"),
                        ),
                    )
                )
                workspace.mark_ready(comparison_exhausted=True)
                directory = workspace.directory
                print(workspace.state, workspace.summary())
            with SyncWorkspace.open(directory) as inspection:
                assert inspection.state == "ready"
                assert len(next(inspection.batches())) == 1
            with SyncWorkspace.open(directory, writable=True) as reopened:
                reopened.dispose(target, discard=True)
            assert not directory.exists()
            assert not tuple(source_root.iterdir()) and not tuple(target_root.iterdir())
            assert source.operations() == target.operations() == ()


if __name__ == "__main__":
    main()
