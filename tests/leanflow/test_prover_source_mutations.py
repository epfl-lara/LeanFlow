"""Exercise real competing editor processes and crash-safe prover source captures."""

from __future__ import annotations

import json
import multiprocessing
import os
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import source_mutation
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.source import SourceConflictError
from leanflow_cli.workflows.prover.source_mutation_recovery import rollback

BEFORE = b"theorem goal : True := by sorry\n"
AFTER = b"theorem goal : True := by trivial\n"
EDIT = b"-- independently saved editor correction\n" + BEFORE


def _mutation_worker(
    root: str, identifier: str, action: str, boundary: str, channel: Connection, crash: bool
) -> None:
    """Stop at one durable filesystem boundary in a separate controller process."""
    project = Path(root)

    def checkpoint(name: str) -> None:
        if name == boundary:
            channel.send({"boundary": name})
            if crash:
                os._exit(44)
            assert channel.recv() == "continue"

    try:
        if action == "install":
            source_mutation.install(project, identifier, checkpoint)
            source_mutation.commit(project, identifier)
        else:
            rollback(project, identifier, checkpoint)
        channel.send({"status": "integrated" if action == "install" else "rolled_back"})
    except SourceConflictError as error:
        try:
            rollback(project, identifier)
        except SourceConflictError:
            pass
        channel.send({"status": "source_conflict", "error": str(error)})
    finally:
        channel.close()


def _spawn(root: Path, identifier: str, action: str, boundary: str, *, crash: bool = False):
    """Launch a fresh process rather than sharing the controller's Python state."""
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(
        target=_mutation_worker,
        args=(str(root), identifier, action, boundary, remote, crash),
    )
    process.start()
    remote.close()
    assert local.poll(15), "controller did not reach its filesystem boundary"
    assert local.recv() == {"boundary": boundary}
    return process, local


def _finish(process, channel: Connection) -> dict[str, Any]:
    """Require the competing controller to finish and close its actual process."""
    channel.send("continue")
    assert channel.poll(15), "controller did not terminate after the editor save"
    report = channel.recv()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("source mutation controller remained alive")
    assert process.exitcode == 0
    channel.close()
    return report


def _editor_write(path: Path, style: str, descriptor: int | None) -> None:
    """Save through atomic replacement, in-place writing, or a previously open descriptor."""
    if style == "atomic":
        temporary = path.with_name("independent-editor-save.tmp")
        temporary.write_bytes(EDIT)
        temporary.replace(path)
    elif style == "inplace":
        path.write_bytes(EDIT)
    else:
        assert descriptor is not None
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, EDIT)
        os.ftruncate(descriptor, len(EDIT))
        os.fsync(descriptor)


def _preserved(root: Path, identifier: str) -> None:
    """Require exact user bytes at the source or an explicitly identified retained inode."""
    target = root / "Main.lean"
    retained = source_mutation.directory(root, identifier)
    locations = [target, *retained.iterdir()]
    assert any(path.is_file() and path.read_bytes() == EDIT for path in locations)
    record = source_mutation.load(root, identifier)
    assert record["state"] == "conflicted"
    assert "conflict" in record
    assert record["conflict_paths"]["rollback_directory"] == str(retained)
    assert record["conflict_paths"]["rollback_versions"] == record["rollback_slots"]
    assert all((retained / name).is_file() for name in record["rollback_slots"])
    # Repeated restart cannot turn unresolved versions into successful integration.
    with pytest.raises(SourceConflictError):
        rollback(root, identifier)
    assert any(path.is_file() and path.read_bytes() == EDIT for path in locations)


@pytest.mark.parametrize("boundary", ["capture_pending", "rollback_capture_pending"])
def test_oversized_captured_editor_inode_is_restored_canonical(tmp_path: Path, boundary: str):
    """Keep the actual editor version at the source even when bounded hashing refuses it."""
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    identifier = source_mutation.create(tmp_path, target.name, BEFORE, AFTER)
    if boundary == "rollback_capture_pending":
        source_mutation.install(tmp_path, identifier)
    process, channel = _spawn(
        tmp_path,
        identifier,
        "install" if boundary == "capture_pending" else "rollback",
        boundary,
    )
    oversized = b"x" * (source_mutation.FILE_LIMIT + 1)
    target.write_bytes(oversized)
    inode = target.stat().st_ino
    report = _finish(process, channel)
    assert report["status"] == "source_conflict"
    assert target.read_bytes() == oversized and target.stat().st_ino == inode
    assert source_mutation.load(tmp_path, identifier)["state"] == "conflicted"


@pytest.mark.parametrize("phase", ["filesystem", "rename_api"])
def test_unsupported_mutation_preflight_leaves_canonical_untouched(tmp_path, monkeypatch, phase):
    """Reject unsupported hosts or rename APIs before capturing the user's inode."""
    from leanflow_cli.formalization.publication_io import PublicationUnsupported

    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    inode = target.stat().st_ino

    def unsupported(*_):
        raise PublicationUnsupported("unsupported no-replace filesystem")

    monkeypatch.setattr(
        source_mutation,
        "check_filesystem" if phase == "filesystem" else "rename_exclusive",
        unsupported,
    )
    with pytest.raises(PublicationUnsupported):
        source_mutation.create(tmp_path, target.name, BEFORE, AFTER)
    assert target.read_bytes() == BEFORE and target.stat().st_ino == inode
    assert not list((tmp_path / source_mutation.BASE).glob("*/displaced"))


@pytest.mark.parametrize(
    ("boundary", "style"),
    [
        ("capture_pending", "atomic"),
        ("capture_pending", "inplace"),
        ("captured", "descriptor"),
        ("install_pending", "atomic"),
        ("installed", "atomic"),
        ("installed", "inplace"),
        ("installed", "descriptor"),
    ],
)
def test_competing_editor_install_never_loses_actual_version(
    tmp_path: Path, boundary: str, style: str
) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    descriptor = os.open(target, os.O_RDWR) if style == "descriptor" else None
    identifier = source_mutation.create(tmp_path, "Main.lean", BEFORE, AFTER)
    try:
        process, channel = _spawn(tmp_path, identifier, "install", boundary)
        _editor_write(target, style, descriptor)
        report = _finish(process, channel)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    assert report["status"] == "source_conflict"
    assert str(source_mutation.directory(tmp_path, identifier)) in report["error"]
    _preserved(tmp_path, identifier)


@pytest.mark.parametrize(
    ("boundary", "style"),
    [
        ("rollback_capture_pending", "atomic"),
        ("rollback_capture_pending", "inplace"),
        ("rollback_captured", "descriptor"),
        ("restore_pending", "atomic"),
        ("restored", "descriptor"),
    ],
)
def test_competing_editor_rollback_never_loses_actual_version(
    tmp_path: Path, boundary: str, style: str
) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    identifier = source_mutation.create(tmp_path, "Main.lean", BEFORE, AFTER)
    source_mutation.install(tmp_path, identifier)
    descriptor = os.open(target, os.O_RDWR) if style == "descriptor" else None
    try:
        process, channel = _spawn(tmp_path, identifier, "rollback", boundary)
        _editor_write(target, style, descriptor)
        report = _finish(process, channel)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    assert report["status"] == "source_conflict"
    _preserved(tmp_path, identifier)


@pytest.mark.parametrize(
    "boundary", ["capture_pending", "captured", "install_pending", "installed"]
)
def test_killed_install_recovers_idempotently(tmp_path: Path, boundary: str) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    original_inode = target.stat().st_ino
    identifier = source_mutation.create(tmp_path, "Main.lean", BEFORE, AFTER)
    process, channel = _spawn(tmp_path, identifier, "install", boundary, crash=True)
    process.join(15)
    assert process.exitcode == 44
    channel.close()
    rollback(tmp_path, identifier)
    rollback(tmp_path, identifier)
    assert target.read_bytes() == BEFORE and target.stat().st_ino == original_inode


@pytest.mark.parametrize(
    "boundary", ["rollback_capture_pending", "rollback_captured", "restore_pending", "restored"]
)
def test_killed_rollback_recovers_idempotently(tmp_path: Path, boundary: str) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    original_inode = target.stat().st_ino
    identifier = source_mutation.create(tmp_path, "Main.lean", BEFORE, AFTER)
    source_mutation.install(tmp_path, identifier)
    process, channel = _spawn(tmp_path, identifier, "rollback", boundary, crash=True)
    process.join(15)
    assert process.exitcode == 44
    channel.close()
    rollback(tmp_path, identifier)
    rollback(tmp_path, identifier)
    assert target.read_bytes() == BEFORE and target.stat().st_ino == original_inode


def test_retained_original_preserves_descriptor_writes_after_commit(tmp_path: Path) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    descriptor = os.open(target, os.O_RDWR)
    identifier = source_mutation.create(tmp_path, "Main.lean", BEFORE, AFTER)
    try:
        source_mutation.install(tmp_path, identifier)
        source_mutation.commit(tmp_path, identifier)
        _editor_write(target, "descriptor", descriptor)
    finally:
        os.close(descriptor)
    assert target.read_bytes() == AFTER
    with pytest.raises(SourceConflictError, match="late edit retained"):
        source_mutation.audit_retained(tmp_path, identifier)
    _preserved(tmp_path, identifier)


class _Verifier:
    """Allow independent-check flow while exercising real source filesystem mutations."""

    def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"accepted": True}

    def compile_module(self, relative: str) -> dict[str, Any]:
        return {"accepted": True}


def _runtime_worker(root: str, phase: str, channel: Connection) -> None:
    """Exercise installation or scoped rollback through the production runtime call site."""
    from leanflow_cli.workflows.prover import scoped_cache, source_transaction

    project = Path(root)
    runtime = ProverRuntime(
        root=project,
        targets=[project / "Main.lean"],
        config=ProverConfig(),
        session=lambda **kwargs: {},
        verifier=_Verifier(),
    )
    runtime.verifier.scoped_modules = {} if phase.endswith("rollback") else None
    original_install = source_mutation.install

    def install(root: Path, identifier: str, boundary=None) -> None:
        def checkpoint(name: str) -> None:
            if name == "capture_pending" and phase == "install":
                channel.send({"boundary": name, "run_directory": str(runtime.store.directory)})
                assert channel.recv() == "continue"

        original_install(root, identifier, checkpoint)

    source_mutation.install = install
    if phase.endswith("rollback"):

        def barrier(*args: Any, **kwargs: Any) -> None:
            channel.send(
                {"boundary": "cache_barrier", "run_directory": str(runtime.store.directory)}
            )
            assert channel.recv() == "continue"

        scoped_cache.mark_cache_refresh_needed = barrier
        runtime.verifier.compile_module = lambda relative: {
            "accepted": False,
            "error": "build failed",
        }
    try:
        if phase == "legacy_rollback":
            document = runtime.documents["Main.lean"]
            source_transaction.begin_proof(
                runtime, runtime.dag.nodes[0], ["trivial"], document, AFTER.decode()
            )
            (project / "Main.lean").write_bytes(AFTER)
            source_transaction.recover_proof(runtime)
        else:
            runtime._accept(runtime.dag.nodes[0], ["trivial"], "controller")
        channel.send({"status": "integrated"})
    except Exception as error:
        channel.send(
            {
                "status": getattr(
                    error,
                    "status",
                    "source_conflict" if isinstance(error, SourceConflictError) else "error",
                ),
                "error": str(error),
            }
        )
    channel.close()


@pytest.mark.parametrize("phase", ["install", "rollback", "legacy_rollback"])
@pytest.mark.parametrize("style", ["atomic", "inplace", "descriptor"])
def test_runtime_never_reports_integration_over_two_process_editor_save(
    tmp_path: Path, phase: str, style: str
) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    descriptor = os.open(target, os.O_RDWR) if style == "descriptor" else None
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(target=_runtime_worker, args=(str(tmp_path), phase, remote))
    process.start()
    remote.close()
    assert local.poll(15)
    checkpoint = local.recv()
    assert checkpoint["boundary"] == ("capture_pending" if phase == "install" else "cache_barrier")
    try:
        _editor_write(target, style, descriptor)
        report = _finish(process, local)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    assert report["status"] == "source_conflict"
    events = Path(checkpoint["run_directory"]) / "events.jsonl"
    assert '"proof_integrated"' not in (events.read_text() if events.exists() else "")
    if phase == "legacy_rollback":
        assert target.read_bytes() == EDIT
        assert (Path(checkpoint["run_directory"]) / "source-transaction.json").exists()
        return
    transactions = list((tmp_path / source_mutation.BASE).iterdir())
    assert len(transactions) == 1
    _preserved(tmp_path, transactions[0].name)
    # Controller acceptance does not erase the independently edited source version.
    assert "retained versions:" in report["error"]
    journal = source_mutation.load(tmp_path, transactions[0].name)
    assert journal["state"] == "conflicted"


def test_equal_editor_bytes_in_a_new_inode_are_not_adopted_for_rollback(tmp_path: Path) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    identifier = source_mutation.create(tmp_path, "Main.lean", BEFORE, AFTER)
    source_mutation.install(tmp_path, identifier)
    editor = tmp_path / "editor.tmp"
    editor.write_bytes(AFTER)
    editor.replace(target)
    inode = target.stat().st_ino
    with pytest.raises(SourceConflictError, match="independent editor destination"):
        rollback(tmp_path, identifier)
    assert target.read_bytes() == AFTER and target.stat().st_ino == inode


@pytest.mark.parametrize("after", [None, b"new helper\n"])
def test_absent_and_deleted_sources_use_the_same_capture_contract(
    tmp_path: Path, after: bytes | None
) -> None:
    target = tmp_path / "Helper.lean"
    before = BEFORE if after is None else None
    if before is not None:
        target.write_bytes(before)
    identifier = source_mutation.create(tmp_path, target.name, before, after)
    source_mutation.install(tmp_path, identifier)
    rollback(tmp_path, identifier)
    rollback(tmp_path, identifier)
    assert (target.read_bytes() if target.exists() else None) == before


def test_malformed_or_missing_mutation_identity_never_widens_source_scope(tmp_path: Path) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    with pytest.raises(ValueError):
        rollback(tmp_path, "../Main.lean")
    identifier = source_mutation.create(tmp_path, target.name, BEFORE, AFTER)
    path = source_mutation.directory(tmp_path, identifier) / "journal.json"
    record = json.loads(path.read_text())
    record["path"] = "../other.lean"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        rollback(tmp_path, identifier)
    path.unlink()
    with pytest.raises(SourceConflictError, match="journal is unavailable"):
        rollback(tmp_path, identifier)
    assert target.read_bytes() == BEFORE


def _cancelled_waiter(root: str, channel: Connection) -> None:
    """Cancel from a live receiver while the controller actually waits in flock."""
    import threading

    from leanflow_cli.formalization import publication_io

    project = Path(root)
    runtime = ProverRuntime(
        root=project,
        targets=[project / "Main.lean"],
        config=ProverConfig(),
        session=lambda **kwargs: {},
        verifier=_Verifier(),
    )
    channel.send({"status": "ready"})
    assert channel.recv() == "start"
    flock = publication_io.fcntl.flock
    announced = False

    def acquiring(descriptor: int, operation: int) -> Any:
        nonlocal announced
        if not announced:
            channel.send({"status": "waiting"})
            announced = True
        return flock(descriptor, operation)

    publication_io.fcntl.flock = acquiring

    def cancel() -> None:
        assert channel.recv() == "cancel"
        runtime.cancelled.set()
        channel.send({"status": "cancelled"})

    receiver = threading.Thread(target=cancel)
    receiver.start()
    try:
        runtime._accept(runtime.dag.nodes[0], ["trivial"], "controller")
        channel.send({"status": "integrated"})
    except Exception as error:
        channel.send(
            {
                "status": "stopped",
                "error": str(error),
                "node_status": runtime.dag.nodes[0].status,
                "run_directory": str(runtime.store.directory),
            }
        )
    receiver.join(5)
    channel.close()


def test_cancelled_waiter_does_not_promote_or_mutate_after_publication_lock_release(
    tmp_path: Path,
) -> None:
    from leanflow_cli.formalization.publication_io import publication_lock

    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(target=_cancelled_waiter, args=(str(tmp_path), remote))
    process.start()
    remote.close()
    assert local.poll(15) and local.recv() == {"status": "ready"}
    with publication_lock(tmp_path):
        local.send("start")
        assert local.poll(15) and local.recv() == {"status": "waiting"}
        local.send("cancel")
        assert local.poll(15) and local.recv() == {"status": "cancelled"}
        assert target.read_bytes() == BEFORE
    assert local.poll(15)
    result = local.recv()
    process.join(15)
    assert process.exitcode == 0
    local.close()
    assert result["status"] == "stopped"
    assert "interrupted" in result["error"].lower()
    assert result["node_status"] != "proved"
    assert target.read_bytes() == BEFORE
    assert not (tmp_path / source_mutation.BASE).exists()
    events = Path(result["run_directory"]) / "events.jsonl"
    assert '"candidate_checked"' not in (events.read_text() if events.exists() else "")
    assert not (Path(result["run_directory"]) / "source-transaction.json").exists()


def test_editor_replacement_and_late_installed_descriptor_preserve_both_versions(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(BEFORE)
    identifier = source_mutation.create(tmp_path, target.name, BEFORE, AFTER)
    process, channel = _spawn(tmp_path, identifier, "install", "installed")
    descriptor = os.open(target, os.O_RDWR)
    late = b"-- late save through installed descriptor\n" + AFTER
    try:
        _editor_write(target, "atomic", None)
        os.write(descriptor, late)
        os.ftruncate(descriptor, len(late))
        os.fsync(descriptor)
        report = _finish(process, channel)
    finally:
        os.close(descriptor)
    assert report["status"] == "source_conflict"
    assert target.read_bytes() == EDIT
    assert (source_mutation.directory(tmp_path, identifier) / "candidate").read_bytes() == late
    _preserved(tmp_path, identifier)
