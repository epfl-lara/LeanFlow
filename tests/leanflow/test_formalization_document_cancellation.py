"""Exercise real queued document ownership without releasing an unrelated holder."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import threading
import time
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from leanflow_cli.formalization import records
from leanflow_cli.formalization.records import DocumentLockCancelled, document_lock


def _waiter(root: str, channel: Connection, mode: str) -> None:
    """Report cancellation while the parent's separate descriptor retains ownership."""
    cancelled = threading.Event()
    actual = fcntl.flock
    announced = False

    def flock(descriptor: int, operation: int) -> None:
        nonlocal announced
        if not announced:
            channel.send("waiting")
            announced = True
        actual(descriptor, operation)

    records.fcntl.flock = flock
    listener = None
    if mode == "cancel":

        def receive() -> None:
            assert channel.recv() == "cancel"
            cancelled.set()

        listener = threading.Thread(target=receive)
        listener.start()
    started = time.monotonic()
    try:
        with document_lock(
            Path(root),
            "doc-test",
            cancelled=cancelled.is_set if mode == "cancel" else None,
            deadline=time.monotonic() + 0.2 if mode == "deadline" else None,
        ):
            channel.send({"status": "acquired"})
    except DocumentLockCancelled:
        channel.send({"status": "cancelled", "elapsed": time.monotonic() - started})
    if listener is not None:
        listener.join(2)
    channel.close()


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
def test_queued_document_waiter_exits_without_holder_release(tmp_path: Path, mode: str) -> None:
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    with document_lock(tmp_path, "doc-test") as directory:
        process = context.Process(target=_waiter, args=(str(tmp_path), remote, mode))
        process.start()
        remote.close()
        try:
            assert local.poll(15) and local.recv() == "waiting"
            if mode == "cancel":
                local.send("cancel")
            assert local.poll(2), "Document cancellation waited for unrelated holder release"
            result = local.recv()
            process.join(2)
            assert process.exitcode == 0
            assert result["status"] == "cancelled"
            assert result["elapsed"] < 1
            with (directory / "document.lock").open("a") as separate:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(separate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(2)
            local.close()


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf")])
def test_invalid_document_deadline_precedes_effects(tmp_path: Path, deadline: float) -> None:
    with pytest.raises(ValueError, match="finite absolute deadline"):
        with document_lock(tmp_path, "doc-test", deadline=deadline):
            pytest.fail("Invalid work must not acquire")
    assert not (tmp_path / ".leanflow").exists()


def test_document_precancellation_precedes_effects(tmp_path: Path) -> None:
    with pytest.raises(DocumentLockCancelled):
        with document_lock(tmp_path, "doc-test", cancelled=lambda: True):
            pytest.fail("Cancelled work must not acquire")
    assert not (tmp_path / ".leanflow").exists()


def test_cancellation_at_document_acquire_closes_its_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = fcntl.flock
    cancelled = False

    def acquire(descriptor: int, operation: int) -> None:
        nonlocal cancelled
        actual(descriptor, operation)
        cancelled = True

    monkeypatch.setattr(records.fcntl, "flock", acquire)
    with pytest.raises(DocumentLockCancelled):
        with document_lock(tmp_path, "doc-test", cancelled=lambda: cancelled):
            pytest.fail("Cancelled work must not run caller effects")
    with (tmp_path / ".leanflow/formalizations/doc-test/document.lock").open("a") as handle:
        actual(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual(handle.fileno(), fcntl.LOCK_UN)


def _malformed_waiter(root: str, channel: Connection, bounded: bool) -> None:
    """Exercise the actual control-file open rather than substituting a Python exception."""
    channel.send("opening")
    started = time.monotonic()
    try:
        with document_lock(
            Path(root), "doc-test", deadline=time.monotonic() + 0.2 if bounded else None
        ):
            channel.send({"status": "acquired"})
    except (OSError, ValueError, DocumentLockCancelled) as error:
        channel.send(
            {"status": "rejected", "error": str(error), "elapsed": time.monotonic() - started}
        )
    channel.close()


@pytest.mark.parametrize("reader", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
def test_fifo_document_control_never_blocks_open(
    tmp_path: Path, reader: bool, bounded: bool
) -> None:
    directory = tmp_path / ".leanflow/formalizations/doc-test"
    directory.mkdir(parents=True)
    fifo = directory / "document.lock"
    os.mkfifo(fifo)
    descriptor = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK) if reader else None
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(target=_malformed_waiter, args=(str(tmp_path), remote, bounded))
    process.start()
    remote.close()
    try:
        assert local.poll(15) and local.recv() == "opening"
        assert local.poll(2), "Malformed control blocked before cancellation polling"
        result = local.recv()
        process.join(2)
        assert process.exitcode == 0
        assert result["status"] == "rejected"
        assert result["elapsed"] < 1
        assert fifo.is_fifo()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        if descriptor is not None:
            os.close(descriptor)
        local.close()


def test_document_lock_hardlink_is_rejected_without_changing_either_entry(tmp_path: Path) -> None:
    directory = tmp_path / ".leanflow/formalizations/doc-test"
    directory.mkdir(parents=True)
    path = directory / "document.lock"
    path.write_bytes(b"preserved")
    alias = tmp_path / "aliased-control"
    alias.hardlink_to(path)
    with pytest.raises(ValueError, match="aliased"):
        with document_lock(tmp_path, "doc-test"):
            pytest.fail("Aliased control must not grant ownership")
    assert path.read_bytes() == alias.read_bytes() == b"preserved"


def test_replaced_document_lock_at_acquisition_refuses_caller_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = fcntl.flock
    directory = tmp_path / ".leanflow/formalizations/doc-test"

    def acquire(descriptor: int, operation: int) -> None:
        actual(descriptor, operation)
        path = directory / "document.lock"
        path.rename(directory / "displaced.lock")
        path.write_bytes(b"replacement")

    monkeypatch.setattr(records.fcntl, "flock", acquire)
    with pytest.raises(ValueError, match="changed"):
        with document_lock(tmp_path, "doc-test", deadline=time.monotonic() + 1):
            pytest.fail("Replaced lock must not grant caller ownership")
    assert (directory / "document.lock").read_bytes() == b"replacement"
    with (directory / "displaced.lock").open("a") as displaced:
        actual(displaced.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual(displaced.fileno(), fcntl.LOCK_UN)


def test_replaced_document_lock_during_open_refuses_fresh_inode_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / ".leanflow/formalizations/doc-test"
    directory.mkdir(parents=True)
    path = directory / "document.lock"
    path.write_bytes(b"initial")
    actual = os.open

    def opening(location: Path, flags: int, mode: int = 0o777) -> int:
        if location == path:
            path.rename(directory / "displaced.lock")
            path.write_bytes(b"replacement")
        return actual(location, flags, mode)

    monkeypatch.setattr(records.os, "open", opening)
    with pytest.raises(ValueError, match="changed"):
        with document_lock(tmp_path, "doc-test"):
            pytest.fail("Replacement observed during open must not grant ownership")
    assert (directory / "displaced.lock").read_bytes() == b"initial"
    assert path.read_bytes() == b"replacement"
