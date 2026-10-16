"""Refuse cancelled formalization consumers before selecting or publishing any evidence."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import threading
import time
from contextlib import nullcontext
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from core.deadlines import operation_cancellation
from leanflow_cli.formalization import proposal, publication_io, publish
from leanflow_cli.formalization.publication_io import PublicationCancelled, publication_lock
from leanflow_cli.formalization.records import (
    DocumentLockCancelled,
    create_document,
    current_selection_token,
    document_lock,
    load_document,
)


def _evidence(root: Path) -> dict[str, bytes]:
    """Exclude ownership files while pinning all source, selection and durable evidence bytes."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    }


def _publish_fixture(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Retain real receipt writes while avoiding unrelated Lean and source-review execution."""
    record.update(
        units=[],
        checks={"declarations": [], "obligations": []},
        project_source_hashes={},
        allowed_axioms=[],
        environment_id="fixture-environment",
    )
    return {
        "accepted": True,
        "record": record,
        "selection_token": current_selection_token(root, record["document_id"]),
        "frozen_root": str(root),
        "files": {},
        "environment_generation": "fixture-generation",
    }


def _stub_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate admission from unrelated compiled source-review fixture requirements."""
    for module, name in (
        ("source_reconciliation", "assert_reconciliation_reviewed"),
        ("source_visual_receipt", "validated_visual_evidence"),
        ("slice_gate", "assert_complete_slice"),
    ):
        monkeypatch.setattr(f"leanflow_cli.formalization.{module}.{name}", lambda *_args: [])
    monkeypatch.setattr(publish, "build_handoff", lambda *_args, **_kwargs: {})


def _invoke(
    root: Path,
    record: dict[str, Any],
    operation: str,
    *,
    cancelled: Any = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Exercise the public consumers with real durable document state."""
    if operation == "proposal":
        return proposal.check_proposal(
            root=root,
            snapshot=root / "author",
            baseline={},
            directory=root / ".leanflow/checks",
            document_id=record["document_id"],
            base_revision_id=record["revision_id"],
            selection_token=current_selection_token(root, record["document_id"]),
            allowed_axioms=(),
            deadline=deadline,
            cancelled=cancelled,
        )
    checked = _publish_fixture(root, record)
    return publish.publish_checked(
        root,
        {},
        {},
        checked,
        {"accepted": True, "reviews": []},
        root / ".leanflow/publication",
        cancelled=cancelled,
        **({"deadline": deadline} if deadline is not None else {}),
    )


@pytest.mark.parametrize("operation", ["proposal", "publish"])
@pytest.mark.parametrize("mode", ["cancel", "context", "deadline"])
def test_cancelled_admission_keeps_all_evidence_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, mode: str
) -> None:
    """Reject stopped work before creating a submission, receipt or publication plan."""
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")
    before = _evidence(tmp_path)
    # Restore the publication-only stubs even when the call raises before admission.
    with monkeypatch.context() as scoped:
        _stub_publication(scoped)
        context = operation_cancellation(lambda: True) if mode == "context" else nullcontext()
        with context:
            options = {
                "cancelled": (lambda: True) if mode == "cancel" else None,
                "deadline": time.monotonic() - 1 if mode == "deadline" else None,
            }
            if operation == "proposal":
                result = _invoke(tmp_path, record, operation, **options)
                assert result["error_code"] == (
                    "check_timeout" if mode == "deadline" else "check_cancelled"
                )
                assert "revision_id" not in result
            else:
                with pytest.raises((PublicationCancelled, DocumentLockCancelled)):
                    _invoke(tmp_path, record, operation, **options)
    assert _evidence(tmp_path) == before


def _queued_consumer(
    root_value: str,
    document_id: str,
    channel: Connection,
    operation: str,
    held: str,
    mode: str,
) -> None:
    """Wait on an actual unrelated owner and report termination without holder release."""
    root = Path(root_value)
    record = load_document(root, document_id)
    stopped = threading.Event()
    actual = fcntl.flock
    target = (
        root / ".leanflow/formalization-publication.lock"
        if held == "project"
        else Path(record["_directory"]) / "document.lock"
    )
    target_identity = (target.stat().st_dev, target.stat().st_ino)
    announced = False

    def observed(descriptor: int, flags: int) -> Any:
        nonlocal announced
        info = os.fstat(descriptor)
        if not announced and (info.st_dev, info.st_ino) == target_identity:
            channel.send("waiting")
            announced = True
        return actual(descriptor, flags)

    publication_io.fcntl.flock = observed
    listener = None
    if mode != "deadline":

        def receive() -> None:
            assert channel.recv() == "cancel"
            stopped.set()

        listener = threading.Thread(target=receive, daemon=True)
        listener.start()
    started = time.monotonic()
    context = operation_cancellation(stopped.is_set) if mode == "context" else nullcontext()
    patches = pytest.MonkeyPatch()
    _stub_publication(patches)
    try:
        with context:
            result = _invoke(
                root,
                record,
                operation,
                cancelled=stopped.is_set if mode == "cancel" else None,
                deadline=started + 0.3 if mode == "deadline" else None,
            )
            channel.send({"result": result, "elapsed": time.monotonic() - started})
    except (PublicationCancelled, DocumentLockCancelled) as error:
        channel.send({"cancelled": str(error), "elapsed": time.monotonic() - started})
    finally:
        patches.undo()
        if listener is not None:
            listener.join(2)
        channel.close()


@pytest.mark.parametrize("operation", ["proposal", "publish"])
@pytest.mark.parametrize("held", ["project", "document"])
@pytest.mark.parametrize("mode", ["cancel", "context", "deadline"])
def test_queued_consumer_stops_before_holder_release_and_cannot_resume_mutation(
    tmp_path: Path, operation: str, held: str, mode: str
) -> None:
    """Keep both lock order and immutable authority when cancelled contenders later lose contention."""
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")
    before = _evidence(tmp_path)
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    holder = (
        publication_lock(tmp_path)
        if held == "project"
        else document_lock(tmp_path, record["document_id"])
    )
    process = context.Process(
        target=_queued_consumer,
        args=(str(tmp_path), record["document_id"], remote, operation, held, mode),
    )
    try:
        with holder:
            process.start()
            remote.close()
            assert local.poll(15) and local.recv() == "waiting"
            if mode != "deadline":
                local.send("cancel")
            assert local.poll(2), "Stopped consumer waited for the unrelated lock holder"
            result = local.recv()
            assert result["elapsed"] < 1
            if operation == "proposal":
                assert result["result"]["error_code"] == (
                    "check_timeout" if mode == "deadline" else "check_cancelled"
                )
                assert "revision_id" not in result["result"]
            else:
                assert "cancelled" in result
            process.join(2)
            assert process.exitcode == 0
            assert _evidence(tmp_path) == before
        assert _evidence(tmp_path) == before
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        local.close()


@pytest.mark.parametrize("operation", ["proposal", "publish"])
@pytest.mark.parametrize("acquired", ["project", "document"])
@pytest.mark.parametrize("mode", ["cancel", "context", "deadline"])
def test_stop_at_either_acquisition_releases_both_locks_before_any_evidence_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    acquired: str,
    mode: str,
) -> None:
    """Exercise cancellation delivered by the successful flock, including between both locks."""
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")
    with publication_lock(tmp_path), document_lock(tmp_path, record["document_id"]):
        pass
    paths = {
        "project": tmp_path / ".leanflow/formalization-publication.lock",
        "document": Path(record["_directory"]) / "document.lock",
    }
    identity = (paths[acquired].stat().st_dev, paths[acquired].stat().st_ino)
    before = _evidence(tmp_path)
    clock = [0.0]
    stopped = threading.Event()
    actual = fcntl.flock

    def observed(descriptor: int, flags: int) -> Any:
        result = actual(descriptor, flags)
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == identity:
            stopped.set()
            clock[0] = 2.0
        return result

    _stub_publication(monkeypatch)
    monkeypatch.setattr(publication_io.fcntl, "flock", observed)
    if mode == "deadline":
        monkeypatch.setattr("core.deadlines.time.monotonic", lambda: clock[0])
    context = operation_cancellation(stopped.is_set) if mode == "context" else nullcontext()
    with context:
        options = {
            "cancelled": stopped.is_set if mode == "cancel" else None,
            "deadline": 1.0 if mode == "deadline" else None,
        }
        if operation == "proposal":
            result = _invoke(tmp_path, record, operation, **options)
            assert result["error_code"] == (
                "check_timeout" if mode == "deadline" else "check_cancelled"
            )
            assert "revision_id" not in result
        else:
            with pytest.raises((PublicationCancelled, DocumentLockCancelled)):
                _invoke(tmp_path, record, operation, **options)
    assert _evidence(tmp_path) == before
    for path in paths.values():
        with path.open("a") as descriptor:
            actual(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            actual(descriptor.fileno(), fcntl.LOCK_UN)


@pytest.mark.parametrize("mode", ["cancel", "context", "deadline"])
def test_stop_while_preparing_handoff_cannot_create_accepted_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Recheck Stop after potentially costly preparation and before the first acceptance write."""
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")
    before = _evidence(tmp_path)
    stopped = threading.Event()
    clock = [0.0]
    _stub_publication(monkeypatch)

    def handoff(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        stopped.set()
        clock[0] = 2.0
        return {}

    monkeypatch.setattr(publish, "build_handoff", handoff)
    if mode == "deadline":
        monkeypatch.setattr("core.deadlines.time.monotonic", lambda: clock[0])
    context = operation_cancellation(stopped.is_set) if mode == "context" else nullcontext()
    with context, pytest.raises(PublicationCancelled):
        _invoke(
            tmp_path,
            record,
            "publish",
            cancelled=stopped.is_set if mode == "cancel" else None,
            deadline=1.0 if mode == "deadline" else None,
        )
    assert _evidence(tmp_path) == before
