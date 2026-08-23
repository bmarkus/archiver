from __future__ import annotations

from pathlib import Path

import pytest

from archiver.cli import main


def test_catalog_init_rejects_non_directory_roots_and_control_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root_file = tmp_path / "root-file"
    root_file.write_bytes(b"source")

    assert main(["catalog", "init", str(root_file)]) == 1
    assert "catalog root is not a directory" in capsys.readouterr().err
    assert root_file.read_bytes() == b"source"

    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"source")
    control_file = root / ".archiver"
    control_file.write_bytes(b"not a directory")

    assert main(["catalog", "init", str(root)]) == 1
    assert "catalog control path is not a directory" in capsys.readouterr().err
    assert source.read_bytes() == b"source"
    assert control_file.read_bytes() == b"not a directory"
    assert not (root / ".archiver" / "catalog.sqlite").exists()


def test_catalog_init_rejects_symlinked_root_and_control_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    source = target_root / "source.txt"
    source.write_bytes(b"source")
    root_link = tmp_path / "root-link"
    control_target = tmp_path / "control-target"
    control_target.mkdir()
    control_link_root = tmp_path / "control-link-root"
    control_link_root.mkdir()
    try:
        root_link.symlink_to(target_root, target_is_directory=True)
        (control_link_root / ".archiver").symlink_to(control_target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this platform")

    assert main(["catalog", "init", str(root_link)]) == 1
    assert "catalog root must not be a symbolic link" in capsys.readouterr().err
    assert source.read_bytes() == b"source"
    assert not (target_root / ".archiver").exists()

    assert main(["catalog", "init", str(control_link_root)]) == 1
    assert "catalog control directory must not be a symbolic link" in capsys.readouterr().err
    assert not (control_target / "catalog.sqlite").exists()
