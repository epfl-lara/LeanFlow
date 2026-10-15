"""Count current setup identities while retaining escaped and uncertain ownership evidence."""

from __future__ import annotations

import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from agent.runtime.harness.processes import ProcessTree
from leanflow_cli.workflows.project_setup import execution, public_process
from leanflow_cli.workflows.project_setup.process import SetupFailure


def test_public_command_does_not_count_more_than_256_proven_exited_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    original = execution.owned_rss_bytes

    def accumulated(child, tree):
        value = original(child, tree)
        tree.descendants.update({1_000_000_000 + i: f"old-{i}" for i in range(300)})
        return value

    monkeypatch.setattr(execution, "owned_rss_bytes", accumulated)
    code, output = public_process.run_public_command(
        [sys.executable, "-c", "import time; time.sleep(.35); print('complete')"],
        cwd=tmp_path,
        log=StringIO(),
        cancel=None,
        progress=None,
        phase="sequential-child-regression",
        stdout_only=True,
    )
    assert code == 0 and output.strip() == "complete"


def test_300_sequential_current_identities_stay_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    root = os.getpid()
    tree = ProcessTree(root)
    tree.root_identity = "root"
    table = {root: (root, "S", "root")}
    monkeypatch.setattr(public_process, "_cleanup_states", lambda timeout: table.copy())
    for index in range(300):
        pid = 1_000_000_000 + index
        tree.descendants[pid] = f"child-{index}"
        table = {root: (root, "S", "root"), pid: (root, "S", f"child-{index}")}
        observed = public_process._current_observed_processes(tree)
        assert observed == {str(root): "root", str(pid): f"child-{index}"}
        assert len(tree.descendants) == 1


def test_real_escaped_child_remains_in_current_evidence() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        table = public_process._cleanup_states(2)
        assert table[child.pid][0] == child.pid
        tree = ProcessTree(os.getpid(), {child.pid: table[child.pid][2]})
        tree.root_identity = table[os.getpid()][2]
        observed = public_process._current_observed_processes(tree)
        assert observed[str(child.pid)] == table[child.pid][2]
        assert child.pid in tree.descendants and child.poll() is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)


def test_present_uncertain_start_is_retained_but_absent_and_zombie_are_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ProcessTree(11, {12: "old timestamp", 13: "gone", 14: "exited"})
    tree.root_identity = "original-root"
    table = {11: (11, "Z", "root"), 12: (12, "S", "different timezone"), 14: (11, "Z", "exited")}
    monkeypatch.setattr(public_process, "_cleanup_states", lambda timeout: table)
    assert public_process._current_observed_processes(tree) == {
        "11": "original-root",
        "12": "old timestamp",
    }
    assert tree.descendants == {12: "old timestamp"}


def test_failed_snapshot_never_prunes_cached_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = ProcessTree(11, {12: "escaped", 13: "unknown"})

    def unavailable(timeout):
        raise ValueError("Incomplete process table")

    monkeypatch.setattr(public_process, "_cleanup_states", unavailable)
    with pytest.raises(ValueError, match="Incomplete"):
        public_process._current_observed_processes(tree)
    assert tree.descendants == {12: "escaped", 13: "unknown"}


def test_concurrent_current_identities_still_enforce_256_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    original_rss, original_table = execution.owned_rss_bytes, public_process._cleanup_states
    live = {}

    def concurrent(child, tree):
        value = original_rss(child, tree)
        live.update({1_000_000_000 + i: (child.pid, "S", f"live-{i}") for i in range(256)})
        tree.descendants.update({pid: row[2] for pid, row in live.items()})
        return value

    monkeypatch.setattr(execution, "owned_rss_bytes", concurrent)
    monkeypatch.setattr(
        public_process, "_cleanup_states", lambda timeout: {**original_table(timeout), **live}
    )
    with pytest.raises(SetupFailure) as error:
        public_process.run_public_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            log=StringIO(),
            cancel=None,
            progress=None,
            phase="concurrent-bound",
        )
    assert error.value.code == "setup_process_identity_limit"
