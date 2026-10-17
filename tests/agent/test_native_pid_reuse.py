"""Keep native ancestry and group cleanup tied to the original invocation identities."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.runtime.harness import processes
from agent.runtime.harness.processes import ProcessTree


def test_tree_rejects_a_popen_from_another_root() -> None:
    with pytest.raises(ValueError, match="original root Popen"):
        ProcessTree(101, process=SimpleNamespace(pid=202, returncode=None))


def test_bound_tree_never_adopts_a_reused_reaped_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain real descendants without claiming children of a replacement leader PID."""
    leader = SimpleNamespace(pid=101, returncode=None)
    table = {101: (1, "original-root"), 202: (101, "owned-child")}
    monkeypatch.setattr(processes, "_processes", lambda: dict(table))
    tree = ProcessTree(leader.pid, process=leader)
    tree.refresh()
    assert tree.root_identity == "original-root"
    leader.returncode = 0
    table.update({101: (1, "replacement-root"), 303: (101, "unrelated-child")})
    table[404] = (202, "owned-grandchild")
    tree.refresh()
    assert tree.root_identity == "original-root"
    assert tree.descendants == {202: "owned-child", 404: "owned-grandchild"}
    signals = Mock()
    monkeypatch.setattr(processes.os, "kill", signals)
    tree.signal(signal.SIGKILL)
    assert signals.call_args_list == [((202, signal.SIGKILL),), ((404, signal.SIGKILL),)]


def test_already_reaped_root_cannot_establish_a_first_identity(monkeypatch) -> None:
    """Never bind to a replacement when the original child exited before first sampling."""
    leader = SimpleNamespace(pid=101, returncode=0)
    monkeypatch.setattr(
        processes, "_processes", lambda: {101: (1, "replacement"), 303: (101, "unrelated")}
    )
    tree = ProcessTree(leader.pid, process=leader)
    tree.refresh()
    assert tree.root_identity is None and not tree.descendants


@pytest.mark.parametrize("reaped", [False, True])
def test_observed_root_identity_mismatch_permanently_retires_ancestry(monkeypatch, reaped) -> None:
    """A later return of the old timestamp cannot reactivate a retired numeric root."""
    leader = SimpleNamespace(pid=101, returncode=None)
    table = {101: (1, "original")}
    monkeypatch.setattr(processes, "_processes", lambda: dict(table))
    tree = ProcessTree(101, process=leader)
    tree.refresh()
    leader.returncode = 0 if reaped else None
    table.update({101: (1, "replacement"), 303: (101, "unrelated")})
    tree.refresh()
    table[101] = (1, "original")
    tree.refresh()
    assert tree.root_identity == "original" and not tree.descendants


@pytest.mark.parametrize("owned_group,group_allowed", [(101, True), (404, False)])
def test_native_cleanup_never_adopts_replacement_root_but_stops_cached_child(
    monkeypatch: pytest.MonkeyPatch, owned_group: int, group_allowed: bool
) -> None:
    """Only cached matching group membership permits signaling a former root group."""
    leader = SimpleNamespace(pid=101, returncode=0, wait=Mock(return_value=0))
    table = {101: (1, "replacement"), 303: (101, "unrelated"), 202: (1, "owned")}
    states = {
        101: (101, "S", "replacement"),
        303: (101, "S", "unrelated"),
        202: (owned_group, "S", "owned"),
    }
    tree = ProcessTree(101, {202: "owned"}, process=leader)
    monkeypatch.setattr(processes, "_processes", lambda: dict(table))
    monkeypatch.setattr(processes, "_cleanup_states", lambda timeout: dict(states))
    monkeypatch.setattr(processes, "verify_owned_stopped", Mock(return_value=True))
    groups, individuals = Mock(), Mock()
    monkeypatch.setattr(processes, "signal_group", groups)
    monkeypatch.setattr(processes.os, "kill", individuals)
    processes.cleanup_group(leader, tree)
    assert tree.descendants == {202: "owned"}
    assert groups.call_count == 2 * int(group_allowed)
    assert individuals.call_args_list == [((202, signal.SIGTERM),), ((202, signal.SIGKILL),)]


def test_native_cleanup_without_cached_authority_never_signals_reused_group(monkeypatch) -> None:
    """An already reaped Popen alone gives no authority over its former numeric PGID."""
    leader = SimpleNamespace(pid=101, returncode=0, wait=Mock(return_value=0))
    table = {101: (1, "replacement"), 303: (101, "unrelated")}
    tree = ProcessTree(101, process=leader)
    monkeypatch.setattr(processes, "_processes", lambda: table)
    monkeypatch.setattr(processes, "verify_owned_stopped", Mock(return_value=True))
    groups = Mock()
    monkeypatch.setattr(processes, "signal_group", groups)
    processes.cleanup_group(leader, tree)
    groups.assert_not_called()
    assert not tree.descendants


@pytest.mark.skipif(os.name != "posix", reason="Native invocation fixtures require POSIX")
def test_runner_exit_sampling_never_adopts_replacement_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the native runner's actual poll-to-cleanup sequencing with reused metadata."""
    from agent.runtime.harness import HarnessRequest, IsolationPolicy, runner

    captured = []
    policy = IsolationPolicy(tmp_path)
    command = [sys.executable, "-I", "-u", "-c", 'print(\'{"type":"turn.completed"}\')']
    monkeypatch.setattr(
        runner,
        "_prepare_launch",
        lambda request: (command, dict(os.environ), (), command[0], policy),
    )

    def owned_tree(pid, *, process=None):
        assert process is not None, "The runner must bind original Popen ownership before sampling"
        tree = ProcessTree(pid, {202: "owned"}, process=process)
        captured.append(tree)
        return tree

    def table():
        tree = captured[0]
        assert tree.process is not None
        if tree.process.returncode is None:
            return {tree.root: (1, "original"), 202: (tree.root, "owned")}
        return {tree.root: (1, "replacement"), 202: (1, "owned"), 303: (tree.root, "unrelated")}

    def states(timeout):
        tree = captured[0]
        return {tree.root: (tree.root, "S", "replacement"), 202: (404, "S", "owned")}

    monkeypatch.setattr(runner, "ProcessTree", owned_tree)
    monkeypatch.setattr(processes, "_processes", table)
    monkeypatch.setattr(processes, "_cleanup_states", states)
    monkeypatch.setattr(processes, "verify_owned_stopped", lambda *args, **kwargs: True)
    monkeypatch.setattr(runner, "verify_owned_stopped", lambda *args, **kwargs: True)
    signals = Mock()
    monkeypatch.setattr(processes.os, "kill", signals)
    result = runner.run_harness(HarnessRequest("lemex", "fixture", tmp_path, isolation=policy))
    assert result.status == "completed" and result.cleanup_verified
    assert captured and captured[0].descendants == {202: "owned"}
    assert captured[0].process.returncode == 0
    assert signals.call_args_list == [((202, signal.SIGTERM),), ((202, signal.SIGKILL),)]
