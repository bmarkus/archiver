from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast

import pytest

from archiver.hashing import FileChangedDuringHashingError, _hash_stream, hash_file, hash_file_stably


class _RecordingStream:
    def __init__(self, contents: bytes) -> None:
        self._contents = contents
        self._position = 0
        self.requested_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.requested_sizes.append(size)
        if size < 0:
            size = len(self._contents) - self._position
        chunk = self._contents[self._position : self._position + size]
        self._position += len(chunk)
        return chunk


def test_hash_stream_reads_in_bounded_chunks() -> None:
    contents = b"bounded streaming" * 10
    stream = _RecordingStream(contents)

    content_id = _hash_stream(cast(BinaryIO, stream), chunk_size=7)

    assert content_id.digest == sha256(contents).hexdigest()
    assert len(stream.requested_sizes) > 2
    assert set(stream.requested_sizes) == {7}


@pytest.mark.parametrize("chunk_size", (0, -1))
def test_hash_functions_reject_nonpositive_chunk_sizes(tmp_path: Path, chunk_size: int) -> None:
    path = tmp_path / "entry.bin"
    path.write_bytes(b"contents")

    with pytest.raises(ValueError, match="chunk_size must be positive"):
        hash_file(path, chunk_size=chunk_size)
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        hash_file_stably(path, chunk_size=chunk_size)


def test_stable_hash_rejects_path_handle_identity_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "entry.bin"
    path.write_bytes(b"contents")
    original_fstat = os.fstat

    def mismatched_fstat(file_descriptor: int) -> object:
        metadata = original_fstat(file_descriptor)
        return SimpleNamespace(
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino + 1,
            st_mode=metadata.st_mode,
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns,
        )

    monkeypatch.setattr("archiver.hashing.fstat", mismatched_fstat)

    with pytest.raises(FileChangedDuringHashingError, match="changed before hashing"):
        hash_file_stably(path)


def test_stable_hash_rejects_post_read_pathname_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "entry.bin"
    path.write_bytes(b"contents")
    original_stat = Path.stat
    path_stat_calls = 0

    def replaced_path_stat(candidate: Path, *, follow_symlinks: bool = True) -> object:
        nonlocal path_stat_calls
        metadata = original_stat(candidate, follow_symlinks=follow_symlinks)
        if candidate == path:
            path_stat_calls += 1
            if path_stat_calls == 2:
                return SimpleNamespace(
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino + 1,
                    st_mode=metadata.st_mode,
                    st_size=metadata.st_size,
                    st_mtime_ns=metadata.st_mtime_ns,
                )
        return metadata

    monkeypatch.setattr("archiver.hashing.Path.stat", replaced_path_stat)

    with pytest.raises(FileChangedDuringHashingError, match="changed during hashing"):
        hash_file_stably(path)


def test_stable_hash_rejects_mtime_only_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
                st_size=metadata.st_size,
                st_mtime_ns=metadata.st_mtime_ns + 1,
            )
        return metadata

    monkeypatch.setattr("archiver.hashing.fstat", changed_fstat)

    with pytest.raises(FileChangedDuringHashingError, match="changed during hashing"):
        hash_file_stably(path)


def test_stable_hash_rejects_nonregular_path(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(FileChangedDuringHashingError, match="not a regular file"):
        hash_file_stably(directory)


def test_stable_hash_propagates_path_disappearance_after_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "entry.bin"
    path.write_bytes(b"contents")
    original_stat = Path.stat
    path_stat_calls = 0

    def disappearing_path_stat(candidate: Path, *, follow_symlinks: bool = True) -> object:
        nonlocal path_stat_calls
        if candidate == path:
            path_stat_calls += 1
            if path_stat_calls == 2:
                raise FileNotFoundError(path)
        return original_stat(candidate, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("archiver.hashing.Path.stat", disappearing_path_stat)

    with pytest.raises(FileNotFoundError):
        hash_file_stably(path)
