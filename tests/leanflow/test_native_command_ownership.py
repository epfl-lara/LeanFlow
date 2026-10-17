"""Bound native tool command shutdown and retain separate invocation owners until real pipe EOF."""

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
from leanflow_cli.workflows.prover import check_command_cleanup, check_process, native_command

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Owned tool commands require POSIX")


@pytest.mark.parametrize("outcome", ["leader_exit", "cancelled"])
def test_native_command_keeps_distinct_owners_with_unobserved_detached_pipe_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Keep repeated same-workspace commands independently retryable without an unlimited drain."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = workspace / ".command-home"
    monkeypatch.setattr(native_command, "sandbox_command", lambda argv, policy: list(argv))
    monkeypatch.setattr(ProcessTree, "refresh", lambda self: None)
    fixtures = []
    try:
        for index in range(2):
            marker = workspace / f"fixture-{index}.json"
            script = (
                "import json,os,pathlib,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
                "start_new_session=True,stdin=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'child_pid':child.pid,'temporary':os.environ['TMPDIR']}))\n"
                + ("sys.exit(0)\n" if outcome == "leader_exit" else "time.sleep(60)\n")
            )
            started = time.monotonic()
            with pytest.raises(NativeCleanupUnverified, match="output pipes remain open"):
                native_command.run_command(
                    [sys.executable, "-I", "-u", "-c", script, str(marker)],
                    snapshot=tmp_path,
                    directory=directory,
                    owner_workspace=workspace,
                    cancelled=(lambda: marker.exists()) if outcome == "cancelled" else None,
                )
            assert time.monotonic() - started < 6
            data = json.loads(marker.read_text())
            child_pid = data["child_pid"]
            identity = _processes()[child_pid][1]
            owned = ProcessTree(800, {child_pid: identity})
            temporary = Path(data["temporary"])
            owner = check_command_cleanup._RETAINED[temporary]
            fixtures.append((owner, owned, child_pid))
            assert owner.workspace == workspace.resolve() and owner.private.is_dir()
            assert owner.processes_verified and not owner.output_eof_verified
            assert owner.process.returncode is not None
            assert verify_owned_stopped(owner.tree, group=owner.process.pid, timeout_s=1)
        assert fixtures[0][0].private != fixtures[1][0].private
        assert all(owner.private in check_command_cleanup._RETAINED for owner, _, _ in fixtures)
        for _, owned, child_pid in fixtures:
            owned.signal(signal.SIGKILL)
            assert verify_owned_stopped(owned, group=child_pid, timeout_s=2)
        with monkeypatch.context() as patch:
            for owner, _, _ in fixtures:
                patch.setattr(
                    owner,
                    "kill",
                    lambda *args: pytest.fail("An EOF-only retry signaled a former group"),
                )
            check_process.close_check_workers(workspace)
        assert all(owner.closed and not owner.private.exists() for owner, _, _ in fixtures)
        assert directory.is_dir() and workspace.is_dir()
    finally:
        observed = {child_pid for _, _, child_pid in fixtures}
        for marker in workspace.glob("fixture-*.json"):
            data = json.loads(marker.read_text())
            child_pid = data["child_pid"]
            if child_pid not in observed:
                entry = _processes().get(child_pid)
                if entry is not None:
                    ProcessTree(800, {child_pid: entry[1]}).signal(signal.SIGKILL)
        for _, owned, _ in fixtures:
            owned.signal(signal.SIGKILL)
        check_process.close_check_workers(workspace)
