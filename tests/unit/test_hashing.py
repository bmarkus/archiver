import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from archiver import ContentId
from archiver.hashing import FileChangedDuringHashingError, hash_file, hash_file_stably


def test_hash_file_returns_known_sha256_digest(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_bytes(b"abc")

    assert hash_file(path) == ContentId(
        algorithm="sha256",
        digest="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    )


def test_hash_file_handles_empty_files(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.touch()

    assert hash_file(path).digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_hash_depends_on_bytes_not_path_or_metadata(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "nested" / "second"
    second.parent.mkdir()
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")

    assert hash_file(first) == hash_file(second)


def test_content_id_rejects_noncanonical_digest() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        ContentId(algorithm="sha256", digest="A" * 64)


def test_stable_hash_rejects_detectable_file_metadata_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "entry.bin"
    path.write_bytes(b"contents")
    original_fstat = os.fstat
    calls = 0

    def changed_fstat(file_descriptor: int) -> object:
        nonlocal calls
        calls += 1
        metadata = original_fstat(file_descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_mode=metadata.st_mode,
                st_size=metadata.st_size + 1,
                st_mtime_ns=metadata.st_mtime_ns,
            )
        return metadata

    monkeypatch.setattr("archiver.hashing.fstat", changed_fstat)
    with pytest.raises(FileChangedDuringHashingError, match="changed during hashing"):
        hash_file_stably(path)
