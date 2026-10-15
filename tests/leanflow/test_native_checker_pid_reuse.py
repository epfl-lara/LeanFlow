"""Keep checker cleanup retries from adopting or signaling a reused leader PID."""

from __future__ import annotations

import io
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.runtime.harness import processes
from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import ProcessTree
from leanflow_cli.workflows.prover import check_process as checks


@dataclass
class ReapedProcess:
    """Model Popen's cached leader status without creating or signaling any process."""

    pid: int = 101
    returncode: int | None = 0
    stdin: io.BytesIO = field(default_factory=io.BytesIO)
    stdout: io.BytesIO = field(default_factory=io.BytesIO)
    stderr: io.BytesIO = field(default_factory=io.BytesIO)
    waits: list[float | None] = field(default_factory=list)

    def wait(self, timeout: float | None = None) -> int:
        """Return the already-cached exit status without touching a reused PID."""
        self.waits.append(timeout)
        assert self.returncode is not None
        return self.returncode

    def poll(self) -> int | None:
        """Expose only this fake Popen's cached status."""
        return self.returncode

    def kill(self) -> None:
        """Fail if cleanup falls back to signaling the reaped leader."""
        pytest.fail("A reaped leader must never receive an individual PID kill")


def _worker(tmp_path: Path, process: ReapedProcess, tree: ProcessTree) -> checks._CheckWorker:
    """Construct only the state needed to test close and request without worker startup."""
    worker = object.__new__(checks._CheckWorker)
    worker.process = process
    worker.tree = tree
    worker.private = tmp_path / "private-checker"
    worker.private.mkdir()
    (worker.private / "retained.txt").write_text("Retain until physical output EOF")
    worker.closed = False
    worker._close_lock = threading.Lock()
    worker._closing = False
    worker._processes_verified = False
    worker._output_eof_verified = False
    worker.buffer = bytearray()
    worker.errors = bytearray()
    return worker


def _reused_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tree: ProcessTree,
    *,
    owned_start: str | None = None,
    owned_group: int = 101,
) -> tuple[Mock, Mock, Mock, Mock, Mock, Mock]:
    """Present a reused root and new child while retaining only cached descendant authority."""
    parentage = {101: (1, "new-root"), 303: (101, "new-child")}
    states = {101: (101, "S", "new-root"), 303: (101, "S", "new-child")}
    if owned_start is not None:
        parentage[202] = (1, owned_start)
        states[202] = (owned_group, "S", owned_start)
    samples = Mock(return_value=parentage)
    group_samples = Mock(return_value=states)
    groups = Mock()
    individuals = Mock()
    refresh = Mock(wraps=tree.refresh)
    final = Mock(return_value=True)
    monkeypatch.setattr(processes, "_processes", samples)
    monkeypatch.setattr(checks, "_cleanup_states", group_samples, raising=False)
    monkeypatch.setattr(checks, "signal_group", groups)
    monkeypatch.setattr(processes.os, "kill", individuals)
    monkeypatch.setattr(tree, "refresh", refresh)
    monkeypatch.setattr(checks, "verify_owned_stopped", final)
    return samples, group_samples, groups, individuals, refresh, final


def test_reaped_leader_cleanup_does_not_signal_reused_group_or_adopt_new_children(monkeypatch):
    process = ReapedProcess()
    tree = ProcessTree(process.pid, {202: "owned-child"})
    samples, group_samples, groups, individuals, refresh, final = _reused_metadata(
        monkeypatch, tree
    )
    checks._kill(process, tree)
    refresh.assert_not_called()
    group_samples.assert_called()
    groups.assert_not_called()
    individuals.assert_not_called()
    assert tree.descendants == {202: "owned-child"} and 303 not in tree.descendants
    assert samples.call_count <= 1  # Only cached-identity signaling may sample parentage.
    final.assert_called_once_with(tree, group=process.pid)
    assert process.waits == [5]


@pytest.mark.parametrize(
    "observed_start,observed_group,group_allowed,child_allowed",
    [
        ("owned-child", 101, True, True),
        ("replacement-child", 101, False, False),
        ("owned-child", 404, False, True),
    ],
)
def test_reaped_group_signal_requires_cached_identity_in_that_exact_group(
    monkeypatch, observed_start, observed_group, group_allowed, child_allowed
):
    process = ReapedProcess()
    tree = ProcessTree(process.pid, {202: "owned-child"})
    _, _, groups, individuals, refresh, _ = _reused_metadata(
        monkeypatch, tree, owned_start=observed_start, owned_group=observed_group
    )
    checks._kill(process, tree)
    refresh.assert_not_called()
    assert groups.call_count == int(group_allowed)
    if group_allowed:
        groups.assert_called_once_with(process, signal.SIGKILL)
    assert individuals.call_count == int(child_allowed)
    if child_allowed:
        individuals.assert_called_once_with(202, signal.SIGKILL)
    assert tree.descendants == {202: "owned-child"}


@pytest.mark.parametrize("failure", ["metadata_error", "final_verification_false"])
def test_reaped_cleanup_cannot_certify_unknown_metadata_or_live_final_group(monkeypatch, failure):
    process = ReapedProcess()
    tree = ProcessTree(process.pid, {202: "owned-child"})
    _, group_samples, groups, individuals, refresh, final = _reused_metadata(monkeypatch, tree)
    if failure == "metadata_error":
        group_samples.side_effect = OSError("No independent process metadata")
    else:
        final.return_value = False
    with pytest.raises(NativeCleanupUnverified):
        checks._kill(process, tree)
    refresh.assert_not_called()
    groups.assert_not_called()
    individuals.assert_not_called()
    assert tree.descendants == {202: "owned-child"}


def test_eof_retry_uses_cached_process_verification_without_repeating_kill_or_tracking(
    tmp_path, monkeypatch
):
    process = ReapedProcess()
    tree = ProcessTree(process.pid, {202: "owned-child"})
    worker = _worker(tmp_path, process, tree)
    kill = Mock()
    eof = Mock(side_effect=[NativeCleanupUnverified("Output writer still owns a pipe"), None])
    monkeypatch.setattr(checks, "_kill", kill)
    monkeypatch.setattr(checks, "_require_output_eof", eof)
    monkeypatch.setattr(tree, "refresh", Mock(side_effect=AssertionError("Reused root sampled")))
    monkeypatch.setattr(checks, "signal_group", Mock(side_effect=AssertionError("Reused group")))
    monkeypatch.setattr(processes.os, "kill", Mock(side_effect=AssertionError("Reused child")))
    with pytest.raises(NativeCleanupUnverified, match="Output writer"):
        worker.close()
    assert not worker.closed and worker._closing and worker._processes_verified
    assert worker.private.is_dir()
    assert not process.stdout.closed and not process.stderr.closed
    worker.close()
    kill.assert_called_once_with(process, tree)
    assert eof.call_count == 2
    assert worker.closed and not worker.private.exists()
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_failed_process_verification_is_not_cached_for_the_next_close(tmp_path, monkeypatch):
    process = ReapedProcess()
    tree = ProcessTree(process.pid)
    worker = _worker(tmp_path, process, tree)
    kill = Mock(side_effect=[NativeCleanupUnverified("Process absence unknown"), None])
    eof = Mock()
    monkeypatch.setattr(checks, "_kill", kill)
    monkeypatch.setattr(checks, "_require_output_eof", eof)
    with pytest.raises(NativeCleanupUnverified, match="Process absence unknown"):
        worker.close()
    assert not worker.closed and worker._closing and not worker._processes_verified
    assert worker.private.is_dir()
    eof.assert_not_called()
    worker.close()
    assert kill.call_count == 2 and eof.call_count == 1
    assert worker.closed and not worker.private.exists()


@pytest.mark.parametrize("state", ["failed_closing", "reaped"])
def test_direct_request_rejects_closing_or_reaped_worker_before_io_and_sampling(
    tmp_path, monkeypatch, state
):
    process = ReapedProcess(returncode=None if state == "failed_closing" else 0)
    tree = ProcessTree(process.pid)
    worker = _worker(tmp_path, process, tree)
    worker._closing = state == "failed_closing"
    forbidden = Mock(side_effect=AssertionError("A stopped checker admitted work"))
    monkeypatch.setattr(tree, "refresh", forbidden)
    monkeypatch.setattr(checks.os, "set_blocking", forbidden)
    monkeypatch.setattr(checks, "_kill", forbidden)
    monkeypatch.setattr(worker, "close", forbidden)
    with pytest.raises(NativeCleanupUnverified):
        worker.request({"id": "must-not-start"}, 1)
    forbidden.assert_not_called()
    assert worker.private.is_dir() and not process.stdin.closed


@pytest.mark.parametrize("close_succeeds", [False, True])
def test_check_scratch_retires_closing_worker_before_admitting_a_replacement(
    tmp_path, monkeypatch, close_succeeds
):
    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    source = project / "Main.lean"
    original = "theorem fixtureSource : True := by trivial\n"
    source.write_text(original)
    project, workspace = project.resolve(), workspace.resolve()
    key = project, workspace
    process = ReapedProcess()
    tree = ProcessTree(process.pid)
    prior = _worker(tmp_path, process, tree)
    prior._closing = True
    prior.environment_generation = "unchanged-environment"
    events = []

    def close_prior() -> None:
        events.append("close")
        assert checks._WORKERS[key] is prior
        if not close_succeeds:
            raise NativeCleanupUnverified("The prior checker still owns its output pipes")
        prior.closed = True

    def replacement_request(request: dict[str, object], timeout_s: float | None) -> dict[str, bool]:
        events.append("request")
        assert prior.closed and events == ["close", "construct", "request"]
        assert request["file"] == str(source.resolve())
        assert timeout_s is not None and 0 < timeout_s <= 1
        return {"success": True, "ok": True}

    replacement = SimpleNamespace(
        closed=False,
        _closing=False,
        environment_generation="",
        has_checked=False,
        request=Mock(side_effect=replacement_request),
    )

    def construct(project_arg: Path, workspace_arg: Path) -> SimpleNamespace:
        events.append("construct")
        assert prior.closed and (project_arg, workspace_arg) == key
        return replacement

    constructor = Mock(side_effect=construct)
    forbidden = Mock(side_effect=AssertionError("The closing worker admitted a check"))
    monkeypatch.setattr(prior, "close", Mock(side_effect=close_prior))
    monkeypatch.setattr(prior, "request", forbidden)
    monkeypatch.setattr(tree, "refresh", forbidden)
    monkeypatch.setattr(checks, "_kill", forbidden)
    monkeypatch.setattr(checks, "_CheckWorker", constructor)
    monkeypatch.setattr(checks, "_WORKERS", {key: prior})
    monkeypatch.setattr(checks, "_WORKSPACE_LOCKS", {})
    monkeypatch.setattr(checks, "_validated_paths", lambda *_: key)
    monkeypatch.setattr(
        checks, "lean_environment_generation", lambda *_, **__: "unchanged-environment"
    )
    monkeypatch.setattr(checks, "operation_cancelled", lambda: False)

    if close_succeeds:
        result = checks.check_scratch(
            project_root=project, workspace=workspace, file=source, timeout_s=1
        )
        assert result["success"] and result["ok"] and result["cold_start"]
        assert checks._WORKERS[key] is replacement
        assert events == ["close", "construct", "request"]
        constructor.assert_called_once_with(project, workspace)
        replacement.request.assert_called_once()
    else:
        with pytest.raises(NativeCleanupUnverified, match="still owns its output pipes"):
            checks.check_scratch(
                project_root=project, workspace=workspace, file=source, timeout_s=1
            )
        assert checks._WORKERS[key] is prior and not prior.closed
        assert prior.private.is_dir() and events == ["close"]
        constructor.assert_not_called()
        replacement.request.assert_not_called()
    forbidden.assert_not_called()
    assert source.read_text() == original
    assert checks._WORKSPACE_LOCKS[key].acquire(blocking=False)
    checks._WORKSPACE_LOCKS[key].release()
