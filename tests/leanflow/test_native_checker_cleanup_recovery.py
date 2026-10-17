"""Retain owned checker inputs and bridge authority until retryable physical shutdown succeeds."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent.runtime.harness import processes as native_processes
from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import ProcessTree, verify_owned_stopped
from core.utils import atomic_json_write
from leanflow_cli.native.harness_source_draft import closed_draft_bridge, drain_author_bridge
from leanflow_cli.workflows.prover import check_process as checks
from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.session_tools import SessionTools

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Owned checker process groups require POSIX"
)


@dataclass
class OwnedChecker:
    """Keep one real fixture process tree bound to its actual checker registry and bridge."""

    project: Path
    workspace: Path
    worker: checks._CheckWorker
    bridge: NativeBridge
    child_pid: int

    @property
    def key(self) -> tuple[Path, Path]:
        """Return the exact registry identity owned by this fixture."""
        return self.project.resolve(), self.workspace.resolve()


@pytest.fixture
def owned_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedChecker]:
    """Use small Python sleepers in place of Lean while keeping real ownership and HTTP closure."""
    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (project / "Main.lean").write_text("theorem fixtureSource : True := by sorry\n")
    (project / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    sources = {path.name: path.read_bytes() for path in project.iterdir()}
    script = (
        "import json,os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'child_pid':child.pid}))\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(
        checks,
        "_sandbox_command",
        lambda *args, private, **kwargs: [
            sys.executable,
            "-I",
            "-u",
            "-c",
            script,
            str(private / "started.json"),
        ],
    )
    worker = checks._CheckWorker(project, workspace)
    bridge = None
    try:
        marker = worker.private / "started.json"
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "The fixture checker did not create its owned child"
        metadata = json.loads(marker.read_text())
        assert worker.process is not None and worker.process.pid == metadata["pid"]
        assert worker.tree is not None
        worker.tree.refresh()
        assert metadata["child_pid"] in worker.tree.descendants
        (worker.private / "retained.txt").write_text(
            "Private checker evidence must survive uncertain cleanup"
        )
        tools = SessionTools(role="prover", project_root=project, workspace=workspace, context={})
        bridge = NativeBridge(
            toolset=tools, context={}, callbacks={}, directory=tmp_path / "bridge"
        )
        fixture = OwnedChecker(project, workspace, worker, bridge, metadata["child_pid"])
        with checks._LOCK:
            assert fixture.key not in checks._WORKERS
            checks._WORKERS[fixture.key] = worker
        yield fixture
    finally:
        # Per-test failure patches use independent contexts and are restored before fixture cleanup.
        if bridge is not None:
            bridge.close()
        worker.close()
        checks.close_check_workers(workspace)
        assert worker.closed and worker.process is not None and worker.process.poll() is not None
        assert not worker.private.exists()
        assert {path.name: path.read_bytes() for path in project.iterdir()} == sources


def _state(path: Path, *, draft_only: bool) -> dict:
    """Declare verified author shutdown while retaining the independent checker closure boundary."""
    state = {
        "native_process_cleanup_verified": True,
        "native_cleanup_unverified": True,
        "source_draft_only": draft_only,
        "source_draft_native_closed": draft_only,
        "source_draft_quiescent": False,
    }
    atomic_json_write(path, state)
    return state


def _assert_retained(fixture: OwnedChecker) -> None:
    """Require registry ownership and private bytes to survive a failed physical checker close."""
    assert checks._WORKERS.get(fixture.key) is fixture.worker
    assert not fixture.worker.closed and fixture.worker.private.is_dir()
    assert (fixture.worker.private / "retained.txt").read_text() == (
        "Private checker evidence must survive uncertain cleanup"
    )
    assert not fixture.bridge.closed and fixture.bridge.closing
    assert fixture.bridge.thread.is_alive()
    with pytest.raises(ValueError, match="bridge is closed"):
        fixture.bridge.call("get_assignment", {})
    assert not fixture.bridge.operations and not fixture.bridge.threads


def _assert_closed(fixture: OwnedChecker) -> None:
    """Confirm independent owned absence before accepting the closed flags and deleted private paths."""
    process = fixture.worker.process
    assert process is not None and process.poll() is not None
    assert verify_owned_stopped(fixture.worker.tree, group=process.pid, timeout_s=1)
    assert fixture.worker.closed and fixture.key not in checks._WORKERS
    assert not fixture.worker.private.exists()
    assert fixture.bridge.closed and not fixture.bridge.closing
    assert not fixture.bridge.thread.is_alive()


def _assert_live_owned_child(tree: ProcessTree, child_pid: int) -> None:
    """Observe the exact live fixture child without a 50ms whole-table absence deadline."""
    table = native_processes._cleanup_states(timeout_s=2)
    assert child_pid in tree.descendants and child_pid in table
    group, state, started = table[child_pid]
    assert group == child_pid
    assert started == " ".join(tree.descendants[child_pid].split())
    assert not state.startswith("Z")


@pytest.mark.parametrize("kind", ["live", "missing", "identity", "zombie", "group"])
def test_live_owned_child_observation_requires_matching_identity_and_live_group(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    child_pid, identity = 811, "Thu Oct 15 10:00:00 2026"
    tree = ProcessTree(800, {child_pid: identity})
    table = {
        child_pid: (
            900 if kind == "group" else child_pid,
            "Z+" if kind == "zombie" else "S",
            "Thu Oct 15 11:00:00 2026" if kind == "identity" else identity,
        )
    }
    if kind == "missing":
        table.clear()
    timeouts = []

    def observe(timeout_s: float) -> dict[int, tuple[int, str, str]]:
        timeouts.append(timeout_s)
        return table

    monkeypatch.setattr(native_processes, "_cleanup_states", observe)
    if kind == "live":
        _assert_live_owned_child(tree, child_pid)
    else:
        with pytest.raises(AssertionError):
            _assert_live_owned_child(tree, child_pid)
    assert timeouts == [2]


@pytest.mark.parametrize(
    "error", [ValueError("Unverifiable process table"), subprocess.TimeoutExpired("ps", 2)]
)
def test_live_owned_child_observation_propagates_unavailable_metadata(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def unavailable(timeout_s: float) -> dict[int, tuple[int, str, str]]:
        assert timeout_s == 2
        raise error

    monkeypatch.setattr(native_processes, "_cleanup_states", unavailable)
    with pytest.raises(type(error)) as caught:
        _assert_live_owned_child(ProcessTree(800, {811: "original identity"}), 811)
    assert caught.value is error


@pytest.mark.parametrize("draft_only", [False, True], ids=["normal", "source-draft"])
def test_failed_repeated_checker_close_keeps_live_ownership_and_state_until_verified_retry(
    owned_checker: OwnedChecker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft_only: bool
) -> None:
    fixture = owned_checker
    state_path = tmp_path / "state.json"
    state = _state(state_path, draft_only=draft_only)
    attempts = []

    def uncertain(process, tree):
        attempts.append(process.pid)
        raise NativeCleanupUnverified("Fixture checker remains live before termination")

    with monkeypatch.context() as patch:
        patch.setattr(checks, "_kill", uncertain)
        for _ in range(2):
            with pytest.raises(NativeCleanupUnverified, match="remains live before termination"):
                with closed_draft_bridge(fixture.bridge, state, state_path, draft_only=draft_only):
                    drain_author_bridge(fixture.bridge, state, state_path, draft_only=draft_only)
            _assert_retained(fixture)
            assert fixture.worker.process is not None and fixture.worker.process.poll() is None
            os.kill(fixture.child_pid, 0)
            saved = json.loads(state_path.read_text())
            assert saved["native_cleanup_unverified"] and saved["native_tool_cleanup_unverified"]
            assert saved["source_draft_quiescent"] is False
            assert saved["native_process_cleanup_verified"] is True
    assert len(attempts) >= 2
    with closed_draft_bridge(fixture.bridge, state, state_path, draft_only=draft_only):
        assert state["native_cleanup_unverified"] is True
    _assert_closed(fixture)
    saved = json.loads(state_path.read_text())
    assert saved["native_cleanup_unverified"] is False
    assert saved["native_tool_cleanup_unverified"] is False
    assert saved["source_draft_quiescent"] is draft_only


@pytest.mark.parametrize("failure", ["refresh", "verification"])
def test_group_is_signaled_before_metadata_failure_and_dead_leader_keeps_retryable_inputs(
    owned_checker: OwnedChecker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    fixture = owned_checker
    state_path = tmp_path / "state.json"
    state = _state(state_path, draft_only=True)
    order = []
    original_signal = checks.signal_group

    def signal_first(process, sig):
        order.append("signal_group")
        original_signal(process, sig)

    def unavailable():
        order.append("refresh")
        raise OSError("Fixture process-table sample is unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(checks, "signal_group", signal_first)
        if failure == "refresh":
            patch.setattr(fixture.worker.tree, "refresh", unavailable)
        else:
            patch.setattr(checks, "verify_owned_stopped", lambda *args, **kwargs: False)
        with pytest.raises(NativeCleanupUnverified):
            with closed_draft_bridge(fixture.bridge, state, state_path, draft_only=True):
                pass
        assert order[0] == "signal_group"
        if failure == "refresh":
            assert order[:2] == ["signal_group", "refresh"]
        _assert_retained(fixture)
        assert fixture.worker.process is not None and fixture.worker.process.poll() is not None
        assert verify_owned_stopped(
            fixture.worker.tree, group=fixture.worker.process.pid, timeout_s=1
        )
        saved = json.loads(state_path.read_text())
        assert saved["native_cleanup_unverified"] and saved["native_tool_cleanup_unverified"]
        assert not saved["source_draft_quiescent"]
    with closed_draft_bridge(fixture.bridge, state, state_path, draft_only=True):
        pass
    _assert_closed(fixture)
    assert not state["native_cleanup_unverified"] and state["source_draft_quiescent"]


@pytest.mark.parametrize("target", ["worker", "bridge"])
def test_concurrent_close_waits_for_the_owned_checker_and_finishes_without_deadlock(
    owned_checker: OwnedChecker, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    fixture = owned_checker
    fixture.bridge.__enter__()
    entered, release, second_started, second_done = (threading.Event() for _ in range(4))
    original_kill = checks._kill
    attempts = []
    errors = []

    def blocked_cleanup(process, tree):
        attempts.append(process.pid)
        entered.set()
        assert release.wait(3), "The test did not release its bounded checker shutdown"
        original_kill(process, tree)

    close = fixture.worker.close if target == "worker" else fixture.bridge.close

    def run_close(*, second=False):
        if second:
            second_started.set()
        try:
            close()
        except BaseException as error:
            errors.append(error)
        finally:
            if second:
                second_done.set()

    first = threading.Thread(target=run_close, daemon=True)
    second = threading.Thread(target=run_close, kwargs={"second": True}, daemon=True)
    with monkeypatch.context() as patch:
        patch.setattr(checks, "_kill", blocked_cleanup)
        try:
            first.start()
            assert entered.wait(2)
            second.start()
            assert second_started.wait(1)
            assert not second_done.wait(0.1), "A concurrent close returned before physical shutdown"
            assert not fixture.worker.closed and fixture.worker.process.poll() is None
            assert (
                fixture.worker.private.is_dir() and checks._WORKERS[fixture.key] is fixture.worker
            )
            if target == "bridge":
                _assert_retained(fixture)
        finally:
            release.set()
            first.join(4)
            if second.ident is not None:
                second.join(4)
        assert not first.is_alive() and not second.is_alive(), "Checker close deadlocked"
        assert not errors and second_done.is_set() and len(attempts) == 1
    fixture.bridge.close()
    _assert_closed(fixture)


def test_bridge_drain_releases_bridge_and_checker_registry_locks_for_an_active_operation(
    owned_checker: OwnedChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow an admitted operation to take both locks while close waits for its actual result."""
    fixture = owned_checker
    fixture.bridge.__enter__()
    operation_started, closing_started, operation_finished = (threading.Event() for _ in range(3))
    errors = []
    original_drain = fixture.bridge.drain

    def submit(response):
        operation_started.set()
        assert closing_started.wait(2), "Bridge close did not reach its drain boundary"
        assert checks._LOCK.acquire(timeout=1), "Bridge drain held the checker registry lock"
        try:
            assert fixture.bridge.lock.acquire(timeout=1), "Bridge drain held its operation lock"
            try:
                operation_finished.set()
            finally:
                fixture.bridge.lock.release()
        finally:
            checks._LOCK.release()
        return {"accepted": False, "feedback": "The owned fixture operation completed"}

    def drain():
        assert fixture.bridge.closing and not fixture.bridge.closed
        closing_started.set()
        original_drain()

    def close():
        try:
            fixture.bridge.close()
        except BaseException as error:
            errors.append(error)

    fixture.bridge.callbacks["submit_report"] = submit
    receipt = fixture.bridge.call("submit_report", {"response": "{}"})
    assert operation_started.wait(1)
    closer = threading.Thread(target=close, daemon=True)
    with monkeypatch.context() as patch:
        patch.setattr(fixture.bridge, "drain", drain)
        try:
            closer.start()
            closer.join(4)
        finally:
            closing_started.set()
            closer.join(3)
        assert not closer.is_alive(), "Bridge drain and operation completion deadlocked"
        assert not errors and operation_finished.is_set()
    operation = fixture.bridge.operations[receipt["operation_id"]]
    assert operation["status"] == "completed" and operation["result"]["accepted"] is False
    assert all(not thread.is_alive() for thread in fixture.bridge.threads)
    _assert_closed(fixture)


def test_cancelled_worker_request_can_close_while_registry_shutdown_waits_for_its_close_lock(
    owned_checker: OwnedChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finish actual request cancellation without acquiring the registry from inside worker close."""
    fixture = owned_checker
    fixture.bridge.__enter__()
    kill_started, release_kill, registry_entered, registry_done = (
        threading.Event() for _ in range(4)
    )
    original_close, original_kill = fixture.worker.close, checks._kill
    results, errors = [], []

    def blocked_kill(process, tree):
        kill_started.set()
        assert release_kill.wait(3), "The test did not release request-owned checker cleanup"
        original_kill(process, tree)

    def close():
        if threading.current_thread().name == "fixture-registry-closer":
            registry_entered.set()
        original_close()

    def cancel_request():
        try:
            results.append(
                fixture.worker.request({"id": "cancelled-fixture"}, 5, cancelled=lambda: True)
            )
        except BaseException as error:
            errors.append(error)

    def close_registry():
        try:
            checks.close_check_workers(fixture.workspace)
        except BaseException as error:
            errors.append(error)
        finally:
            registry_done.set()

    requester = threading.Thread(target=cancel_request, daemon=True)
    closer = threading.Thread(target=close_registry, name="fixture-registry-closer", daemon=True)
    with monkeypatch.context() as patch:
        patch.setattr(checks, "_kill", blocked_kill)
        patch.setattr(fixture.worker, "close", close)
        try:
            requester.start()
            assert kill_started.wait(2)
            closer.start()
            assert registry_entered.wait(1)
            assert not registry_done.wait(
                0.1
            ), "The registry retired a checker before physical shutdown"
            assert not fixture.worker.closed and fixture.worker.process.poll() is None
            assert fixture.worker.private.is_dir()
        finally:
            release_kill.set()
            requester.join(4)
            if closer.ident is not None:
                closer.join(4)
        assert (
            not requester.is_alive() and not closer.is_alive()
        ), "Request and registry closure deadlocked"
        assert not errors and registry_done.is_set()
    assert len(results) == 1 and results[0]["error_code"] == "check_cancelled"
    fixture.bridge.close()
    _assert_closed(fixture)


@pytest.mark.parametrize("held_pipe", ["stdout", "stderr"])
def test_unobserved_owned_session_escape_keeps_checker_inputs_until_physical_output_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_pipe: str
) -> None:
    """Keep unknown pipe writers distinct from a stopped tracked group and local stream closure."""
    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (project / "Main.lean").write_text("theorem fixtureSource : True := by sorry\n")
    (project / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    sources = {path.name: path.read_bytes() for path in project.iterdir()}
    script = (
        "import json,os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,"
        f"stdout={'None' if held_pipe == 'stdout' else 'subprocess.DEVNULL'},"
        f"stderr={'None' if held_pipe == 'stderr' else 'subprocess.DEVNULL'})\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'child_pid':child.pid}))\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(
        checks,
        "_sandbox_command",
        lambda *args, private, **kwargs: [
            sys.executable,
            "-I",
            "-u",
            "-c",
            script,
            str(private / "started.json"),
        ],
    )
    worker = checks._CheckWorker(project, workspace)
    bridge = None
    independently_owned = None
    try:
        marker = worker.private / "started.json"
        deadline = time.monotonic() + 3
        metadata = None
        while time.monotonic() < deadline:
            try:
                metadata = json.loads(marker.read_text())
                break
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.01)
        assert metadata is not None, "The owned escaping child did not become observable"
        assert worker.process is not None and worker.process.pid == metadata["pid"]
        assert worker.tree is not None and not worker.tree.descendants
        child_pid = metadata["child_pid"]
        assert os.getpgid(child_pid) == child_pid
        independently_owned = ProcessTree(worker.process.pid)
        independently_owned.refresh()
        assert child_pid in independently_owned.descendants
        _assert_live_owned_child(independently_owned, child_pid)
        (worker.private / "retained.txt").write_text(
            "Private checker evidence must survive uncertain cleanup"
        )
        bridge = NativeBridge(
            toolset=SessionTools(
                role="prover", project_root=project, workspace=workspace, context={}
            ),
            context={},
            callbacks={},
            directory=tmp_path / "bridge",
        )
        fixture = OwnedChecker(project, workspace, worker, bridge, child_pid)
        with checks._LOCK:
            assert fixture.key not in checks._WORKERS
            checks._WORKERS[fixture.key] = worker
        state_path = tmp_path / "state.json"
        state = _state(state_path, draft_only=True)
        with monkeypatch.context() as patch:
            # The test retains an independent complete identity; only checker sampling misses this child.
            patch.setattr(worker.tree, "refresh", lambda: None)
            started = time.monotonic()
            with pytest.raises(NativeCleanupUnverified, match="output pipes remain open"):
                with closed_draft_bridge(bridge, state, state_path, draft_only=True):
                    pass
            assert (
                time.monotonic() - started < 4
            ), "Physical EOF verification did not respect its bound"
            assert worker.process.poll() is not None
            assert worker._processes_verified and worker._closing
            assert verify_owned_stopped(worker.tree, group=worker.process.pid, timeout_s=1)
            _assert_live_owned_child(independently_owned, child_pid)
            _assert_retained(fixture)
            for stream in (worker.process.stdin, worker.process.stdout, worker.process.stderr):
                assert stream is not None and not stream.closed
            pipe = getattr(worker.process, held_pipe)
            with pytest.raises(BlockingIOError):
                os.read(pipe.fileno(), 1)
            saved = json.loads(state_path.read_text())
            assert saved["native_cleanup_unverified"] and saved["native_tool_cleanup_unverified"]
            assert not saved["source_draft_quiescent"]
        independently_owned.signal(signal.SIGKILL)
        assert verify_owned_stopped(independently_owned, group=child_pid, timeout_s=2)
        with monkeypatch.context() as patch:
            # Closure already passed. Even if the former PID/group were reused,
            # retry may only finish physical EOF and retire retained inputs.
            def no_process_retry(*args, **kwargs):
                pytest.fail("An EOF-only retry touched the former process identity")

            patch.setattr(checks, "_kill", no_process_retry)
            patch.setattr(checks, "signal_group", no_process_retry)
            patch.setattr(worker.tree, "refresh", no_process_retry)
            with closed_draft_bridge(bridge, state, state_path, draft_only=True):
                assert state["native_cleanup_unverified"] is True
        _assert_closed(fixture)
        assert all(
            stream.closed
            for stream in (worker.process.stdin, worker.process.stdout, worker.process.stderr)
        )
        saved = json.loads(state_path.read_text())
        assert (
            not saved["native_cleanup_unverified"] and not saved["native_tool_cleanup_unverified"]
        )
        assert saved["source_draft_quiescent"] is True
    finally:
        if independently_owned is not None:
            independently_owned.signal(signal.SIGKILL)
        if bridge is not None:
            bridge.close()
        worker.close()
        checks.close_check_workers(workspace)
        assert not worker.private.exists()
        assert {path.name: path.read_bytes() for path in project.iterdir()} == sources
