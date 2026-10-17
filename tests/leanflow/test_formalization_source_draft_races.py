"""Reject competing FIFO substitutions and cancel draft retention behind real publication ownership."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import threading
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization import publication_io
from leanflow_cli.formalization.publication_io import PublicationCancelled, publication_lock
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_directory,
)
from leanflow_cli.formalization.source_draft import freeze_preview
from tests.leanflow.test_source_inventory import _pdf

LAYOUT = {"owned_prefix": "Main/Document", "aggregator": "Main/Document.lean"}


def _case(root: Path) -> tuple[dict[str, Any], Path]:
    """Build a real frozen paired bundle and a small unaccepted author artifact."""
    source, pdf = root / "paper.tex", root / "paper.pdf"
    source.write_text(r"\documentclass{article}\begin{theorem}Zero equals zero.\end{theorem}")
    _pdf(pdf, ["Theorem 1. Zero equals zero."])
    record = create_document(root, source, companion_pdf=pdf)
    snapshot = root / ".leanflow/author"
    output = snapshot / "Main/Document/Setup.lean"
    output.parent.mkdir(parents=True)
    output.write_text("def provisional : Nat := 0\n")
    return record, snapshot


def _retain(
    root: str, record: dict[str, Any], snapshot: str, channel: Connection, mode: str
) -> None:
    """Wait at a deterministic real lock or open boundary in an independently spawned process."""
    project, author = Path(root), Path(snapshot)
    cancelled = threading.Event()
    listener = None
    if mode == "cancel":
        actual = fcntl.flock
        announced = False

        def flock(descriptor: int, operation: int) -> Any:
            nonlocal announced
            if not announced:
                channel.send("waiting")
                announced = True
            return actual(descriptor, operation)

        publication_io.fcntl.flock = flock

        def receive() -> None:
            assert channel.recv() == "cancel"
            cancelled.set()

        listener = threading.Thread(target=receive)
        listener.start()
    else:
        actual_open = os.open
        target = (
            project / "paper.tex" if mode == "source_fifo" else author / "Main/Document/Setup.lean"
        )

        def opening(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if Path(path) == target:
                channel.send("before_open")
                assert channel.recv() == "continue"
            return actual_open(path, flags, *args, **kwargs)

        os.open = opening
    started = time.monotonic()
    try:
        freeze_preview(
            project,
            record,
            author,
            layout=LAYOUT,
            run_id="race-preview",
            selection_token=current_selection_token(project, record["document_id"]),
            model_report="Provisional draft only.",
            cancelled=cancelled.is_set,
        )
        channel.send({"status": "unexpectedly_retained"})
    except (OSError, ValueError, PublicationCancelled) as error:
        channel.send(
            {"status": "rejected", "error": str(error), "elapsed": time.monotonic() - started}
        )
    finally:
        if listener is not None:
            listener.join(2)
        channel.close()


def _close(process: Any, channel: Connection) -> None:
    """Reap only this test's owned subprocess even when an assertion fails."""
    if process.is_alive():
        process.kill()
        process.join(5)
    channel.close()


def test_cancelled_preview_waiter_exits_before_unrelated_holder_releases(tmp_path: Path) -> None:
    record, snapshot = _case(tmp_path)
    directory = document_directory(tmp_path, record["document_id"])
    blobs = set((directory / "blobs").iterdir())
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    with publication_lock(tmp_path):
        process = context.Process(
            target=_retain, args=(str(tmp_path), record, str(snapshot), remote, "cancel")
        )
        process.start()
        remote.close()
        try:
            assert local.poll(15) and local.recv() == "waiting"
            local.send("cancel")
            assert local.poll(2), "Cancelled preview waited for unrelated ownership release"
            result = local.recv()
            process.join(2)
            assert process.exitcode == 0 and result["status"] == "rejected"
            assert result["elapsed"] < 1
            assert str(tmp_path.resolve()) in publication_io._owners.held
            assert set((directory / "blobs").iterdir()) == blobs
            assert not (directory / "draft-previews").exists()
        finally:
            _close(process, local)


@pytest.mark.parametrize("mode", ["source_fifo", "output_fifo"])
def test_competing_fifo_cannot_block_source_or_draft_artifact_retention(
    tmp_path: Path, mode: str
) -> None:
    record, snapshot = _case(tmp_path)
    directory = document_directory(tmp_path, record["document_id"])
    blobs = set((directory / "blobs").iterdir())
    target = (
        tmp_path / "paper.tex" if mode == "source_fifo" else snapshot / "Main/Document/Setup.lean"
    )
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(
        target=_retain, args=(str(tmp_path), record, str(snapshot), remote, mode)
    )
    process.start()
    remote.close()
    try:
        assert local.poll(15) and local.recv() == "before_open"
        target.unlink()
        os.mkfifo(target)
        local.send("continue")
        assert local.poll(2), "Draft retention waited on a substituted FIFO without a writer"
        result = local.recv()
        process.join(2)
        assert process.exitcode == 0 and result["status"] == "rejected"
        assert set((directory / "blobs").iterdir()) == blobs
        assert not (directory / "draft-previews").exists()
    finally:
        _close(process, local)
