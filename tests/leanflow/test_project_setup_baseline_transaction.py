"""Preserve real editor saves and interrupted public baseline configuration updates."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
from io import StringIO
from pathlib import Path

import pytest

from leanflow_cli.workflows.project_setup import baseline_transaction as transaction
from leanflow_cli.workflows.project_setup import inspect_project, prepare_project, service
from leanflow_cli.workflows.project_setup.process import SetupCancelled


def _admission_death(root_text: str, home_text: str) -> None:
    """Die after durable command admission but before a subprocess handle exists."""
    from leanflow_cli.workflows.project_setup import public_process

    root = Path(root_text)
    os.environ["LEANFLOW_HOME"] = home_text
    transaction.begin(root)

    original = public_process.subprocess.Popen

    def die(*args: object, **kwargs: object) -> object:
        if args[0] == [sys.executable, "-c", "pass"]:
            os._exit(73)
        return original(*args, **kwargs)

    public_process.subprocess.Popen = die  # type: ignore[assignment]
    with public_process.bind_invocation_root(root):
        public_process.run_public_command(
            [sys.executable, "-c", "pass"],
            cwd=root,
            log=StringIO(),
            cancel=None,
            progress=None,
            phase="test",
        )


def test_missing_admitted_marker_after_owner_death_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting an uncertain invocation cannot turn an interrupted transaction quiescent."""
    from leanflow_cli.workflows.project_setup.public_process import invocation_directory

    root = (tmp_path / "project").resolve()
    root.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    worker = multiprocessing.get_context("spawn").Process(
        target=_admission_death, args=(str(root), str(home))
    )
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and worker.exitcode == 73
    (marker,) = invocation_directory(root).glob("*.json")
    assert json.loads(marker.read_text())["cleanup_verified"] is False
    marker.unlink()
    with pytest.raises(RuntimeError, match="closure|invocation|evidence"):
        transaction.recover_pending(root)
    assert transaction.pending_issue(root)
    assert (root / transaction.BASE / "pending.json").exists()


@pytest.mark.parametrize("after_commit", [False, True])
def test_missing_real_command_marker_never_blesses_finish_or_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    """Bind command identity before spawn and preserve that requirement after commit."""
    from leanflow_cli.workflows.project_setup import public_process

    root = tmp_path.resolve()
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    record = transaction.begin(root)
    transaction.promote(root, record, {}, check=lambda: None)
    with public_process.bind_invocation_root(root):
        code, _ = public_process.run_public_command(
            [sys.executable, "-c", "pass"],
            cwd=root,
            log=StringIO(),
            cancel=None,
            progress=None,
            phase="test",
        )
    assert code == 0
    (marker,) = public_process.invocation_directory(root).glob("*.json")
    if after_commit:
        transaction.finish(root, record)
    marker.unlink()
    if not after_commit:
        with pytest.raises(RuntimeError, match="closure|invocation|evidence"):
            transaction.finish(root, record)
        with pytest.raises(RuntimeError, match="closure|invocation|evidence"):
            transaction.recover_pending(root)
    assert transaction.pending_issue(root)
    assert inspect_project(root)["ready"] is False


@pytest.mark.parametrize("transaction_started", [False, True])
def test_public_invocation_identity_is_durable_before_process_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transaction_started: bool
) -> None:
    """Include template/pretransaction commands without creating a fake project journal."""
    from leanflow_cli.workflows.project_setup import invocation_evidence, public_process

    root = tmp_path.resolve()
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    record = transaction.begin(root) if transaction_started else None
    original = public_process.subprocess.Popen
    observed = []

    def launch(*args: object, **kwargs: object) -> object:
        if args[0] != [sys.executable, "-c", "pass"]:
            return original(*args, **kwargs)  # type: ignore[arg-type]
        (marker,) = public_process.invocation_directory(root).glob("*.json")
        identifier = marker.stem
        value = json.loads(marker.read_text())
        assert value["pid"] is None and value["cleanup_verified"] is False
        assert invocation_evidence.expected(root) == [identifier]
        if record is not None:
            durable = json.loads((root / transaction.BASE / "pending.json").read_text())
            assert durable["invocations"] == [identifier]
        observed.append(identifier)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(public_process.subprocess, "Popen", launch)
    with public_process.bind_invocation_root(root):
        public_process.run_public_command(
            [sys.executable, "-c", "pass"],
            cwd=root,
            log=StringIO(),
            cancel=None,
            progress=None,
            phase="test",
        )
    assert len(observed) == 1
    if record is None:
        record = transaction.begin(root)
        assert record["invocations"] == observed
    transaction.promote(root, record, {}, check=lambda: None)
    transaction.finish(root, record)
    archived = json.loads((root / transaction.BASE / f"{record['id']}.json").read_text())
    assert archived["invocations"] == observed
    (marker,) = public_process.invocation_directory(root).glob("*.json")
    marker.unlink()
    assert transaction.pending_issue(root)


def _crash(root_text: str, boundary: str) -> None:
    """Die at a durable no-clobber boundary in a genuinely separate controller."""
    root = Path(root_text)
    record = transaction.begin(root)
    stage = Path(record["stage"])
    (stage / "lean-toolchain").write_bytes(b"new-toolchain")
    (stage / "lake-manifest.json").write_bytes(b"new-manifest")

    def stop(point: str) -> None:
        if point == boundary:
            os._exit(73)

    transaction.promote(
        root,
        record,
        {"lake-manifest.json": None, "lean-toolchain": b"original-toolchain"},
        check=lambda: None,
        boundary=stop,
    )


def _late_editor(root_text: str, mode: str, opened: object, mutate: object, done: object) -> None:
    """Hold an old descriptor or atomically replace entries at an exact capture boundary."""
    root = Path(root_text)
    handle = (root / "lean-toolchain").open("r+b") if mode == "descriptor" else None
    opened.set()  # type: ignore[attr-defined]
    mutate.wait(10)  # type: ignore[attr-defined]
    try:
        for name in ("lean-toolchain", "lake-manifest.json"):
            data = ("editor-" + name).encode()
            target = root / name
            if handle is not None and name == "lean-toolchain":
                handle.seek(0)
                handle.truncate()
                handle.write(data)
                handle.flush()
            elif mode == "atomic":
                incoming = target.with_suffix(".editor")
                incoming.write_bytes(data)
                os.replace(incoming, target)
            else:
                target.write_bytes(data)
        done.set()  # type: ignore[attr-defined]
    finally:
        if handle:
            handle.close()


def _rollback_editor(root_text: str, atomic: bool, ready: object, done: object) -> None:
    """Save an originally absent manifest while rollback is about to capture it."""
    ready.wait(10)  # type: ignore[attr-defined]
    path = Path(root_text) / "lake-manifest.json"
    if atomic:
        temporary = path.with_suffix(".editor")
        temporary.write_bytes(b"editor-manifest-during-rollback")
        os.replace(temporary, path)
    else:
        path.write_bytes(b"editor-manifest-during-rollback")
    done.set()  # type: ignore[attr-defined]


def _editor(root: str, atomic: bool, ready: object, saved: object) -> None:
    """Save from an independent process after setup has captured its original inputs."""
    ready.wait(10)  # type: ignore[attr-defined]
    for name in ("lake-manifest.json", "lean-toolchain"):
        path = Path(root) / name
        data = ("editor-" + name).encode()
        if atomic:
            temporary = path.with_suffix(".editor")
            temporary.write_bytes(data)
            os.replace(temporary, path)
        else:
            path.write_bytes(data)
    saved.set()  # type: ignore[attr-defined]


@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
def test_public_setup_failure_preserves_second_process_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, atomic: bool, cancelled: bool
) -> None:
    """Never restore old pins over an editor save made while update is running."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "demo"\n')
    originals = {
        "lean-toolchain": b"leanprover/lean4:v4.33.1\n",
        "lake-manifest.json": b'{"packages": []}',
    }
    for name, data in originals.items():
        (root / name).write_bytes(data)
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(service, "find_executable", lambda name: "/tools/" + name)
    context = multiprocessing.get_context("spawn")
    ready, saved = context.Event(), context.Event()
    editor = context.Process(target=_editor, args=(str(root), atomic, ready, saved))
    editor.start()

    def run(command: list[str], **kwargs: object) -> tuple[int, str]:
        if command == ["lake", "update", "repl"]:
            ready.set()
            assert saved.wait(10)
            if cancelled:
                raise SetupCancelled()
            return 1, "dependency update failed"
        return 0, ""

    monkeypatch.setattr(service, "run_command", run)
    try:
        result = prepare_project(root, cancel=threading.Event())
        assert result["status"] != "ready"
        for name in originals:
            assert (root / name).read_bytes() == ("editor-" + name).encode()
        assert inspect_project(root)["ready"] is False
        retained = [path.read_bytes() for path in (root / ".leanflow").rglob("*") if path.is_file()]
        assert all(data in retained for data in originals.values())
    finally:
        ready.set()
        editor.join(10)
        if editor.is_alive():
            editor.terminate()
            editor.join(5)
        assert editor.exitcode == 0


@pytest.mark.parametrize(
    "boundary", ["prepared", "capture_pending", "captured", "install_pending", "installed"]
)
def test_process_death_recovers_original_absence_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Recover old authority and absence without replacing independently created files."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_bytes(b"original-toolchain")
    worker = multiprocessing.get_context("spawn").Process(target=_crash, args=(str(root), boundary))
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and worker.exitcode == 73
    assert transaction.pending_issue(root)
    assert inspect_project(root)["ready"] is False
    transaction.recover_pending(root)
    transaction.recover_pending(root)
    assert (root / "lean-toolchain").read_bytes() == b"original-toolchain"
    assert not (root / "lake-manifest.json").exists()


def test_unknown_closure_does_not_recover_or_delete_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep an interrupted stage and pins when its invocation has no closure proof."""
    from leanflow_cli.workflows.project_setup.public_process import invocation_directory

    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    record = transaction.begin(tmp_path)
    stage = Path(record["stage"])
    retained = stage / "evidence"
    retained.write_bytes(b"required by a potentially live writer")
    commands = invocation_directory(stage)
    commands.mkdir(parents=True)
    (commands / "unknown.json").write_text(json.dumps({"cleanup_verified": False}))
    with pytest.raises(RuntimeError, match="uncertain subprocess closure"):
        transaction.recover_pending(tmp_path)
    assert retained.read_bytes() == b"required by a potentially live writer"


@pytest.mark.parametrize("mode", ["atomic", "inplace", "descriptor"])
@pytest.mark.parametrize("manifest_present", [False, True])
def test_competing_capture_and_recovery_never_overwrite_editor_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, manifest_present: bool
) -> None:
    """Retain original bytes, unknown captures, and original absence across repeated recovery."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    original = {
        "lean-toolchain": b"original-toolchain",
        "lake-manifest.json": b"original-manifest" if manifest_present else None,
    }
    for name, content in original.items():
        if content is not None:
            (root / name).write_bytes(content)
    record = transaction.begin(root)
    stage = Path(record["stage"])
    for name in original:
        (stage / name).write_bytes(("staged-" + name).encode())
    context = multiprocessing.get_context("spawn")
    opened, mutate, done = context.Event(), context.Event(), context.Event()
    editor = context.Process(target=_late_editor, args=(str(root), mode, opened, mutate, done))
    editor.start()
    assert opened.wait(10)
    triggered = False

    def boundary(point: str) -> None:
        nonlocal triggered
        if not triggered and point == ("captured" if mode == "descriptor" else "capture_pending"):
            triggered = True
            mutate.set()
            assert done.wait(10)

    try:
        with pytest.raises(RuntimeError, match="protected source changed"):
            transaction.promote(root, record, original, check=lambda: None, boundary=boundary)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                transaction.recover_pending(root)
            for name in original:
                assert (root / name).read_bytes() == ("editor-" + name).encode()
        assert (
            record["original_inputs"]["lake-manifest.json"] is None
            if not manifest_present
            else record["original_inputs"]["lake-manifest.json"] is not None
        )
        assert inspect_project(root)["ready"] is False
        originals = stage / ".baseline-originals"
        for name, content in original.items():
            if content is not None:
                assert (originals / name).read_bytes() == content
    finally:
        mutate.set()
        editor.join(10)
        if editor.is_alive():
            editor.terminate()
            editor.join(5)
        assert editor.exitcode == 0


@pytest.mark.parametrize("atomic", [False, True])
def test_rollback_capture_retains_second_process_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, atomic: bool
) -> None:
    """An editor winning rollback stays canonical despite original absence."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_bytes(b"original-toolchain")
    context = multiprocessing.get_context("spawn")
    controller = context.Process(target=_crash, args=(str(root), "installed"))
    controller.start()
    controller.join(10)
    assert controller.exitcode == 73
    assert (root / "lake-manifest.json").read_bytes() == b"new-manifest"
    ready, done = context.Event(), context.Event()
    editor = context.Process(target=_rollback_editor, args=(str(root), atomic, ready, done))
    editor.start()

    def save(point: str) -> None:
        if point == "rollback_capture_pending":
            ready.set()
            assert done.wait(10)

    try:
        with pytest.raises(RuntimeError):
            transaction.recover_pending(root, boundary=save)
        assert (root / "lake-manifest.json").read_bytes() == b"editor-manifest-during-rollback"
        assert transaction.pending_issue(root)
        with pytest.raises(RuntimeError):
            transaction.recover_pending(root)
        assert (root / "lake-manifest.json").read_bytes() == b"editor-manifest-during-rollback"
    finally:
        ready.set()
        editor.join(10)
        if editor.is_alive():
            editor.terminate()
            editor.join(5)
        assert editor.exitcode == 0
