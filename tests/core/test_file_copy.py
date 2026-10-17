"""Check copy-on-write isolation, fallback capacity, and atomic directory publication."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import file_copy


def test_real_clone_preserves_bytes_metadata_and_independent_writes(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.write_bytes(b"proof bytes" * 1024)
    original.chmod(0o640)
    os.utime(original, ns=(1_000_000_000, 2_000_000_000))
    copied = tmp_path / "copied"
    file_copy.copy_file(original, copied)
    assert copied.read_bytes() == original.read_bytes()
    assert copied.stat().st_ino != original.stat().st_ino
    assert copied.stat().st_nlink == 1
    assert copied.stat().st_mtime_ns == original.stat().st_mtime_ns
    assert copied.stat().st_mode == original.stat().st_mode
    copied.write_bytes(b"private changes")
    assert original.read_bytes() == b"proof bytes" * 1024


def test_hardlinked_source_never_shares_an_inode_with_copy(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.write_text("canonical")
    linked = tmp_path / "linked"
    os.link(original, linked)
    copied = tmp_path / "copied"
    file_copy.copy_file(linked, copied)
    assert copied.stat().st_nlink == 1
    assert copied.stat().st_ino != linked.stat().st_ino
    copied.write_text("changed")
    assert original.read_text() == "canonical"


def test_unsupported_clone_falls_back_to_independent_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original"
    original.write_text("content")
    monkeypatch.setattr(file_copy, "_clone_file", lambda *_: False)
    copied = tmp_path / "copied"
    file_copy.copy_file(original, copied)
    assert copied.read_text() == "content"
    assert copied.stat().st_ino != original.stat().st_ino


def test_full_clone_never_falls_back_to_a_large_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original"
    original.write_text("content")

    def exhausted(*_: object) -> bool:
        raise OSError(errno.ENOSPC, "No space left")

    monkeypatch.setattr(file_copy, "_clone_file", exhausted)
    with pytest.raises(OSError, match="No space left"):
        file_copy.copy_file(original, tmp_path / "copied")
    assert not (tmp_path / "copied").exists()


def test_fallback_preserves_space_for_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original"
    original.write_text("content")
    monkeypatch.setattr(file_copy, "_clone_file", lambda *_: False)
    monkeypatch.setattr(file_copy.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(OSError, match="checkpoint reserve"):
        file_copy.copy_file(original, tmp_path / "copied")
    assert not (tmp_path / "copied").exists()


def test_existing_destination_and_source_symlinks_are_rejected(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.write_text("canonical")
    copied = tmp_path / "copied"
    copied.write_text("keep")
    with pytest.raises(FileExistsError):
        file_copy.copy_file(original, copied)
    assert copied.read_text() == "keep"
    linked = tmp_path / "linked"
    linked.symlink_to(original)
    with pytest.raises(ValueError, match="symlinks"):
        file_copy.copy_file(linked, tmp_path / "new")


def test_failed_tree_copy_leaves_no_destination_or_staging_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "source"
    original.mkdir()
    (original / "Main.lean").write_text("saved proof")

    def fail(*_: object) -> str:
        raise OSError(errno.ENOSPC, "No space left")

    monkeypatch.setattr(file_copy, "copy_file", fail)
    with pytest.raises(OSError):
        file_copy.copy_tree(original, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()
    assert not list(tmp_path.glob(".copy-*"))
    assert (original / "Main.lean").read_text() == "saved proof"


def test_successful_tree_is_independent_and_complete(tmp_path: Path) -> None:
    original = tmp_path / "source"
    original.mkdir()
    (original / "state.json").write_text('{"run":"saved"}')
    destination = file_copy.copy_tree(original, tmp_path / "copy")
    assert (destination / "state.json").read_bytes() == (original / "state.json").read_bytes()
    (destination / "state.json").write_text("changed")
    assert (original / "state.json").read_text() == '{"run":"saved"}'


def test_filtered_tree_preserves_explicit_reserve_on_copy_fallback(tmp_path, monkeypatch) -> None:
    original = tmp_path / "source"
    original.mkdir()
    (original / "proof.txt").write_text("proof")
    (original / "cache.bin").write_text("ignored")
    monkeypatch.setattr(file_copy, "_clone_file", lambda *_: False)
    monkeypatch.setattr(file_copy.shutil, "disk_usage", lambda _: SimpleNamespace(free=10))
    with pytest.raises(OSError, match="checkpoint reserve"):
        file_copy.copy_tree(
            original,
            tmp_path / "copy",
            ignore=lambda _, names: {"cache.bin"},
            reserve_bytes=10,
        )
    assert not (tmp_path / "copy").exists()
    monkeypatch.setattr(file_copy.shutil, "disk_usage", lambda _: SimpleNamespace(free=100))
    result = file_copy.copy_tree(
        original,
        tmp_path / "copy",
        ignore=lambda _, names: {"cache.bin"},
        reserve_bytes=10,
    )
    assert (result / "proof.txt").read_text() == "proof"
    assert not (result / "cache.bin").exists()


def test_ordinary_copy_polls_controller_between_bounded_chunks_and_cleans_cancelled_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, copied = tmp_path / "original", tmp_path / "copied"
    content = b"p" * (4 * 1024**2)
    original.write_bytes(content)
    monkeypatch.setattr(file_copy, "_clone_file", lambda *_: False)
    observed = []

    def stop_after_first_chunk() -> None:
        size = copied.stat().st_size if copied.exists() else 0
        observed.append(size)
        if size >= 1024**2:
            raise InterruptedError("Copy cancelled")

    with pytest.raises(InterruptedError, match="Copy cancelled"):
        file_copy.copy_file(original, copied, check=stop_after_first_chunk)
    assert 1024**2 in observed
    assert not copied.exists()
    assert original.read_bytes() == content


def test_tree_copy_polls_directories_and_cancels_without_publishing_partial_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, copied = tmp_path / "source", tmp_path / "copied"
    (original / "nested").mkdir(parents=True)
    content = b"p" * (4 * 1024**2)
    (original / "nested/large").write_bytes(content)
    monkeypatch.setattr(file_copy, "_clone_file", lambda *_: False)
    visited = []

    def ignore(path: str, names: list[str]) -> set[str]:
        visited.append(Path(path).name)
        return set()

    def stop_after_first_chunk() -> None:
        for partial_tree in tmp_path.glob(".copy-*/tree"):
            partial = partial_tree / "nested/large"
            if partial.exists() and partial.stat().st_size >= 1024**2:
                raise InterruptedError("Tree copy cancelled")

    with pytest.raises(InterruptedError, match="Tree copy cancelled"):
        file_copy.copy_tree(original, copied, ignore=ignore, check=stop_after_first_chunk)
    assert visited == ["source", "nested"]
    assert not copied.exists() and not list(tmp_path.glob(".copy-*"))
    assert (original / "nested/large").read_bytes() == content


def test_atomic_tree_publication_preserves_concurrent_empty_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    (source / "Main.lean").write_text("saved proof")
    publish = file_copy._rename_tree_no_replace
    created = []

    def create_then_publish(incoming: Path, target: Path) -> None:
        target.mkdir()
        created.append(target.stat().st_ino)
        publish(incoming, target)

    monkeypatch.setattr(file_copy, "_rename_tree_no_replace", create_then_publish)
    with pytest.raises(FileExistsError):
        file_copy.copy_tree(source, destination)
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert destination.stat().st_ino == created[0]
    assert not list(tmp_path.glob(".copy-*"))
    assert (source / "Main.lean").read_text() == "saved proof"
