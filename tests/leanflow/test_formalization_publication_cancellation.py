"""Check optional publication wait cancellation without changing legacy recovery ownership."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization import publication_io
from leanflow_cli.formalization.publication_io import (
    PublicationCancelled,
    PublicationConflict,
    publication_lock,
)


def _waiter(root: str, channel: Connection, mode: str) -> None:
    """Attempt real ownership while another process retains the publication lease."""
    import threading

    cancelled = threading.Event()
    actual = fcntl.flock
    announced = False

    def flock(descriptor: int, operation: int) -> Any:
        nonlocal announced
        if not announced:
            channel.send("waiting")
            announced = True
        return actual(descriptor, operation)

    publication_io.fcntl.flock = flock
    if mode == "cancel":

        def receive() -> None:
            assert channel.recv() == "cancel"
            cancelled.set()

        listener = threading.Thread(target=receive)
        listener.start()
    else:
        listener = None
    started = time.monotonic()
    try:
        with publication_lock(
            Path(root),
            cancelled=cancelled.is_set if mode == "cancel" else None,
            deadline=time.monotonic() + 0.2 if mode == "deadline" else None,
        ):
            channel.send({"status": "acquired"})
    except PublicationCancelled as error:
        channel.send(
            {"status": "cancelled", "error": str(error), "elapsed": time.monotonic() - started}
        )
    if listener is not None:
        listener.join(2)
    channel.close()


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
def test_optional_waiter_exits_while_unrelated_owner_keeps_ownership(
    tmp_path: Path, mode: str
) -> None:
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    with publication_lock(tmp_path):
        process = context.Process(target=_waiter, args=(str(tmp_path), remote, mode))
        process.start()
        remote.close()
        assert local.poll(15) and local.recv() == "waiting"
        if mode == "cancel":
            local.send("cancel")
        assert local.poll(2), "queued cancellation waited for unrelated owner release"
        result = local.recv()
        process.join(2)
        assert process.exitcode == 0
        assert result["status"] == "cancelled"
        assert result["elapsed"] < 1
        # Cancellation did not release the unrelated process's original descriptor.
        descriptor = publication_io._owners.held[str(tmp_path.resolve())][1]
        assert descriptor >= 0
    local.close()


def test_default_reentrant_recovery_remains_permitted_after_cancel(tmp_path: Path) -> None:
    cancelled = False
    with publication_lock(tmp_path, cancelled=lambda: cancelled):
        cancelled = True
        with publication_lock(tmp_path):
            assert str(tmp_path.resolve()) in publication_io._owners.held
        with pytest.raises(PublicationCancelled):
            with publication_lock(tmp_path, cancelled=lambda: cancelled):
                pytest.fail("Cancelled nested work must not begin")
        assert str(tmp_path.resolve()) in publication_io._owners.held
    assert str(tmp_path.resolve()) not in publication_io._owners.held


def test_cancellation_before_open_leaves_no_lock_entry(tmp_path: Path) -> None:
    with pytest.raises(PublicationCancelled):
        with publication_lock(tmp_path, cancelled=lambda: True):
            pytest.fail("Cancelled work must not begin")
    assert not (tmp_path / ".leanflow/formalization-publication.lock").exists()


def test_cancellation_at_acquisition_releases_only_the_new_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = fcntl.flock
    cancelled = False

    def acquire(descriptor: int, operation: int) -> None:
        nonlocal cancelled
        actual(descriptor, operation)
        cancelled = True

    monkeypatch.setattr(publication_io.fcntl, "flock", acquire)
    with pytest.raises(PublicationCancelled):
        with publication_lock(tmp_path, cancelled=lambda: cancelled):
            pytest.fail("Cancellation at acquisition must prevent caller effects")
    assert str(tmp_path.resolve()) not in publication_io._owners.held
    # A separate open description can immediately acquire: no orphan owner remains.
    with (tmp_path / ".leanflow/formalization-publication.lock").open("a") as handle:
        actual(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual(handle.fileno(), fcntl.LOCK_UN)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf")])
def test_invalid_absolute_deadlines_fail_before_ownership(tmp_path: Path, deadline: float) -> None:
    with pytest.raises(ValueError, match="finite absolute deadline"):
        with publication_lock(tmp_path, deadline=deadline):
            pytest.fail("Invalid deadline must not start work")


def _fifo_waiter(root: str, channel: Connection, reader: bool, substitute: bool) -> None:
    """Exercise a real FIFO both before and during the controller's descriptor open."""
    lock = Path(root) / ".leanflow/formalization-publication.lock"
    lock.parent.mkdir(parents=True)
    original_open = os.open
    reader_descriptor = None
    if substitute:
        lock.touch()

        def open_lock(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            nonlocal reader_descriptor
            if Path(path) == lock:
                lock.unlink()
                os.mkfifo(lock)
                if reader:
                    reader_descriptor = original_open(lock, os.O_RDONLY | os.O_NONBLOCK)
            return original_open(path, flags, *args, **kwargs)

        publication_io.os.open = open_lock
    else:
        os.mkfifo(lock)
        if reader:
            reader_descriptor = original_open(lock, os.O_RDONLY | os.O_NONBLOCK)
    channel.send("attempting")
    started = time.monotonic()
    try:
        with publication_lock(Path(root), deadline=time.monotonic() + 0.2):
            channel.send({"status": "acquired"})
    except (OSError, PublicationConflict) as error:
        channel.send(
            {"status": "rejected", "error": str(error), "elapsed": time.monotonic() - started}
        )
    finally:
        if reader_descriptor is not None:
            os.close(reader_descriptor)
        channel.close()


@pytest.mark.parametrize("reader", [False, True])
@pytest.mark.parametrize("substitute", [False, True])
def test_real_fifo_lock_never_blocks_or_grants_publication_ownership(
    tmp_path: Path, reader: bool, substitute: bool
) -> None:
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(target=_fifo_waiter, args=(str(tmp_path), remote, reader, substitute))
    process.start()
    remote.close()
    try:
        assert local.poll(15) and local.recv() == "attempting"
        assert local.poll(2), "Opening the publication FIFO ignored its ownership deadline"
        result = local.recv()
        process.join(2)
        assert process.exitcode == 0
        assert result["status"] == "rejected" and result["elapsed"] < 1
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        local.close()


def test_hardlinked_lock_does_not_grant_publication_ownership(tmp_path: Path) -> None:
    lock = tmp_path / ".leanflow/formalization-publication.lock"
    lock.parent.mkdir()
    lock.touch()
    alias = tmp_path / "alias"
    os.link(lock, alias)
    with pytest.raises(PublicationConflict, match="one link"):
        with publication_lock(tmp_path):
            pytest.fail("An aliased inode must not grant ownership")
    assert str(tmp_path.resolve()) not in publication_io._owners.held
    assert os.stat(lock).st_ino == os.stat(alias).st_ino


@pytest.mark.parametrize("phase", ["open", "acquire"])
def test_replaced_lock_inode_never_grants_publication_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    lock = tmp_path / ".leanflow/formalization-publication.lock"
    lock.parent.mkdir()
    lock.touch()
    original_inode = lock.stat().st_ino
    actual_flock = fcntl.flock
    if phase == "open":
        actual_open = os.open

        def open_lock(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if Path(path) == lock:
                lock.rename(lock.with_suffix(".old"))
                replacement = actual_open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
                os.close(replacement)
            return actual_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(publication_io.os, "open", open_lock)
    else:

        def acquire(descriptor: int, operation: int) -> None:
            actual_flock(descriptor, operation)
            lock.rename(lock.with_suffix(".old"))
            lock.touch()

        monkeypatch.setattr(publication_io.fcntl, "flock", acquire)
    with pytest.raises(PublicationConflict, match="changed"):
        with publication_lock(tmp_path):
            pytest.fail("A stale descriptor must not grant caller effects")
    assert str(tmp_path.resolve()) not in publication_io._owners.held
    assert lock.stat().st_ino != original_inode
    for entry in (lock, lock.with_suffix(".old")):
        with entry.open("a") as handle:
            actual_flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            actual_flock(handle.fileno(), fcntl.LOCK_UN)


def test_reentrant_owner_refuses_replaced_entry_without_releasing_its_descriptor(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".leanflow/formalization-publication.lock"
    identity = str(tmp_path.resolve())
    with publication_lock(tmp_path):
        descriptor = publication_io._owners.held[identity][1]
        lock.rename(lock.with_suffix(".old"))
        lock.touch()
        with pytest.raises(PublicationConflict, match="changed"):
            with publication_lock(tmp_path):
                pytest.fail("Nested recovery must not acquire a replacement lock entry")
        assert publication_io._owners.held[identity][1] == descriptor
        assert os.fstat(descriptor).st_ino == lock.with_suffix(".old").stat().st_ino
