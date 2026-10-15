"""Retain one-shot checker inputs until owned processes and physical output pipes close."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import ProcessTree, _processes, verify_owned_stopped
from core.utils import atomic_json_write
from leanflow_cli.native.harness_source_draft import closed_draft_bridge
from leanflow_cli.workflows.prover import check_process
from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.session_tools import SessionTools

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Pipe writer fixtures require POSIX")


@pytest.mark.parametrize("held_pipe", ["stdout", "stderr"])
@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "leader_exit"])
def test_one_shot_retains_unknown_detached_writer_until_physical_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_pipe: str, outcome: str
) -> None:
    """Observe process absence and pipe closure separately, with a safe EOF-only retry."""
    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (project / "Main.lean").write_text("theorem unchanged : True := by sorry\n")
    source = (project / "Main.lean").read_bytes()
    private_paths = []
    independently_owned = None
    bridge = None
    fixture_marker = workspace / "fixture-child.json"
    script = (
        "import json,os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,"
        f"stdout={'None' if held_pipe == 'stdout' else 'subprocess.DEVNULL'},"
        f"stderr={'None' if held_pipe == 'stderr' else 'subprocess.DEVNULL'})\n"
        "metadata=json.dumps({'pid':os.getpid(),'child_pid':child.pid})\n"
        "pathlib.Path(sys.argv[1]).write_text(metadata)\n"
        "pathlib.Path(sys.argv[2]).write_text(metadata)\n"
        + ("sys.exit(0)\n" if outcome == "leader_exit" else "time.sleep(60)\n")
    )

    def command(argv, *, private, **kwargs):
        private_paths.append(private)
        return [
            sys.executable,
            "-I",
            "-u",
            "-c",
            script,
            str(private / "started.json"),
            str(fixture_marker),
        ]

    def cancelled():
        return outcome == "cancelled" and fixture_marker.exists()

    monkeypatch.setattr(check_process, "_sandbox_command", command)
    if outcome == "timeout":
        capture = check_process._capture_command

        def after_child_started(*args, **kwargs):
            deadline = time.monotonic() + 3
            while not fixture_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert fixture_marker.exists(), "The fixture never created its separate-session child"
            return capture(*args, **kwargs)

        monkeypatch.setattr(check_process, "_capture_command", after_child_started)
    # Only the controller misses the child. The fixture retains its own exact
    # identity before independent cleanup and never signals an arbitrary PID.
    monkeypatch.setattr(ProcessTree, "refresh", lambda self: None)
    try:
        started = time.monotonic()
        with pytest.raises(NativeCleanupUnverified, match="output pipes remain open"):
            check_process.isolated_command(
                project_root=project,
                workspace=workspace,
                argv=[sys.executable, "-I", "-c", "pass"],
                timeout_s=0.4 if outcome == "timeout" else None,
                cancelled=cancelled,
            )
        assert time.monotonic() - started < 6
        assert len(private_paths) == 1
        private = private_paths[0]
        metadata = json.loads((private / "started.json").read_text())
        child_pid = metadata["child_pid"]
        identity = _processes()[child_pid][1]
        assert os.getpgid(child_pid) == child_pid
        independently_owned = ProcessTree(800, {child_pid: identity})
        from leanflow_cli.workflows.prover import check_command_cleanup

        retained = check_command_cleanup._RETAINED[private]
        assert retained.workspace == workspace.resolve()
        assert retained.process.poll() is not None
        assert retained.processes_verified and not retained.output_eof_verified
        assert verify_owned_stopped(retained.tree, group=retained.process.pid, timeout_s=1)
        assert private.is_dir() and workspace.is_dir()
        for stream in (retained.process.stdout, retained.process.stderr):
            assert stream is not None and not stream.closed
        with pytest.raises(BlockingIOError):
            os.read(getattr(retained.process, held_pipe).fileno(), 1)
        if outcome == "cancelled" and held_pipe == "stdout":
            bridge = NativeBridge(
                toolset=SessionTools(
                    role="prover", project_root=project, workspace=workspace, context={}
                ),
                context={},
                callbacks={},
                directory=tmp_path / "bridge",
            )
            state_path = tmp_path / "state.json"
            state = {
                "native_process_cleanup_verified": True,
                "source_draft_only": True,
                "source_draft_native_closed": True,
                "source_draft_quiescent": False,
            }
            atomic_json_write(state_path, state)
            with pytest.raises(NativeCleanupUnverified, match="output pipes remain open"):
                with closed_draft_bridge(bridge, state, state_path, draft_only=True):
                    pass
            saved = json.loads(state_path.read_text())
            assert saved["native_cleanup_unverified"] and saved["native_tool_cleanup_unverified"]
            assert not saved["source_draft_quiescent"]
            assert not bridge.closed and bridge.closing
        else:
            with pytest.raises(NativeCleanupUnverified, match="output pipes remain open"):
                check_process.close_check_workers(workspace)
        assert private.exists() and private in check_command_cleanup._RETAINED
        independently_owned.signal(signal.SIGKILL)
        assert verify_owned_stopped(independently_owned, group=child_pid, timeout_s=2)
        with monkeypatch.context() as patch:
            patch.setattr(
                retained,
                "kill",
                lambda *args: pytest.fail("An EOF-only retry signaled a former numeric identity"),
            )
            if bridge is not None:
                with closed_draft_bridge(bridge, state, state_path, draft_only=True):
                    pass
                saved = json.loads(state_path.read_text())
                assert saved["source_draft_quiescent"] and not saved["native_cleanup_unverified"]
                assert bridge.closed and not bridge.closing
            else:
                check_process.close_check_workers(workspace)
        assert not private.exists() and private not in check_command_cleanup._RETAINED
        assert retained.closed and retained.output_eof_verified
        assert all(stream.closed for stream in (retained.process.stdout, retained.process.stderr))
    finally:
        if independently_owned is None and private_paths:
            marker = fixture_marker
            if marker.exists():
                child_pid = json.loads(marker.read_text())["child_pid"]
                entry = _processes().get(child_pid)
                if entry is not None:
                    independently_owned = ProcessTree(800, {child_pid: entry[1]})
        if independently_owned is not None:
            independently_owned.signal(signal.SIGKILL)
        if bridge is not None:
            bridge.close()
        check_process.close_check_workers(workspace)
        assert (project / "Main.lean").read_bytes() == source


def test_chatty_one_shot_reports_truncation_and_cleans_up(tmp_path, monkeypatch) -> None:
    """Keep bounded output tails distinct from successful complete diagnostics."""
    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    private_paths = []

    def command(argv, *, private, **kwargs):
        private_paths.append(private)
        return list(argv)

    monkeypatch.setattr(check_process, "_sandbox_command", command)
    result = check_process.isolated_command(
        project_root=project,
        workspace=workspace,
        argv=[sys.executable, "-I", "-c", "import os;os.write(1,b'x'*(5*1024*1024))"],
        timeout_s=5,
    )
    assert result["success"] and result["stdout_truncated"]
    assert len(result["stdout"]) == check_process._MAX_MESSAGE
    assert private_paths and not private_paths[0].exists()


@pytest.mark.parametrize("metadata_error", [OSError("unavailable"), ValueError("incomplete")])
def test_one_shot_metadata_failure_retains_retryable_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata_error: Exception
) -> None:
    """An unverifiable process table cannot become cleanup evidence or remove private inputs."""
    from agent.runtime.harness import processes
    from leanflow_cli.workflows.prover import check_command_cleanup

    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    private_paths = []

    def command(argv, *, private, **kwargs):
        private_paths.append(private)
        return list(argv)

    monkeypatch.setattr(check_process, "_sandbox_command", command)
    with monkeypatch.context() as patch:

        def unavailable():
            raise metadata_error

        patch.setattr(processes, "_processes", unavailable)
        with pytest.raises(NativeCleanupUnverified, match="retained command inputs"):
            check_process.isolated_command(
                project_root=project,
                workspace=workspace,
                argv=[sys.executable, "-I", "-c", "import time;time.sleep(60)"],
                timeout_s=2,
            )
        assert private_paths and private_paths[0].is_dir()
        retained = check_command_cleanup._RETAINED[private_paths[0]]
        assert retained.process.returncode is not None
        assert not retained.processes_verified and not retained.closed
    try:
        check_process.close_check_workers(workspace)
        assert not private_paths[0].exists()
        assert retained.closed and retained.output_eof_verified
    finally:
        if retained.process.poll() is None:
            os.killpg(retained.process.pid, signal.SIGKILL)
            retained.process.wait(timeout=2)
        check_process.close_check_workers(workspace)
