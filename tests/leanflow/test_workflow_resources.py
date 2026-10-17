"""Bound aggregate siblings, exclude unrelated branches, and reject acceptance after failure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import ProcessTree
from leanflow_cli.runtime import workflow_resources as resources
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from tests.leanflow.test_prover_runtime import Verifier, project, session


@pytest.fixture
def children():
    """Reap every actual fixture child independently of the monitor under test."""
    processes = []

    def spawn():
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    yield spawn
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def test_actual_siblings_exceed_shared_budget_and_preexisting_child_survives(
    children, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = children()
    cancelled = threading.Event()
    monkeypatch.setattr(resources, "native_memory_limit_bytes", lambda env: 25)
    observed = []
    owned_ids: set[int] = set()

    def rss(identities):
        observed.append(set(identities))
        return 10 * (1 + len(set(identities) & owned_ids))

    monkeypatch.setattr(resources, "observed_descendant_rss_bytes", rss)
    window = resources.WorkflowResourceWindow({}, cancelled, interval_s=0.05)
    window.start()
    first, second = children(), children()
    owned_ids.update({first.pid, second.pid})
    try:
        assert cancelled.wait(5), "The independent sampler failed to stop aggregate pressure"
        with pytest.raises(resources.WorkflowResourceFailure) as error:
            window.check(force=True)
        assert error.value.code == "workflow_memory_limit"
        assert any({os.getpid(), first.pid, second.pid} <= sample for sample in observed)
        assert all(unrelated.pid not in sample for sample in observed)
        window.close()
        assert first.wait(timeout=3) != 0 and second.wait(timeout=3) != 0
        assert unrelated.poll() is None
        assert window.evidence()["peak_owned_rss_bytes"] == 30
        assert window.evidence()["physical_output_eof"] == "independently_required_per_actor"
    finally:
        window.close()


def test_existing_owned_child_is_counted_without_adopting_unrelated_child(
    children, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned, unrelated = children(), children()
    table = resources._processes()
    seen = []
    monkeypatch.setattr(
        resources, "observed_descendant_rss_bytes", lambda ids: seen.append(dict(ids)) or 1
    )
    window = resources.WorkflowResourceWindow(
        {}, threading.Event(), already_owned={owned.pid: table[owned.pid][1]}
    )
    window.start()
    window.close()
    assert owned.pid in seen[0] and unrelated.pid not in seen[0]
    assert owned.wait(timeout=3) != 0
    assert unrelated.poll() is None


def test_metadata_failure_stops_admission_after_finite_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()
    window = resources.WorkflowResourceWindow({}, cancelled)
    window.start()
    attempts = []

    def unavailable(ids):
        attempts.append(ids)
        raise ValueError("Malformed RSS table")

    monkeypatch.setattr(resources, "observed_descendant_rss_bytes", unavailable)
    with pytest.raises(resources.WorkflowResourceFailure) as error:
        window.check(force=True)
    assert error.value.code == "workflow_resource_inspection_failed"
    assert cancelled.is_set() and len(attempts) == 5
    assert window.evidence()["peak_owned_rss_bytes"] > 0
    window.close()


def test_unexpected_sampler_failure_cannot_silently_disable_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()
    window = resources.WorkflowResourceWindow({}, cancelled, interval_s=0.05)
    window.start()
    monkeypatch.setattr(window, "_sample", lambda: (_ for _ in ()).throw(RuntimeError("dead")))
    assert cancelled.wait(3)
    with pytest.raises(resources.WorkflowResourceFailure):
        window.check()
    window.close()


def test_reused_excluded_branch_identity_does_not_authorize_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = os.getpid()
    table = {root: (1, "controller"), 71: (root, "unrelated")}
    monkeypatch.setattr(resources, "_processes", lambda: table.copy())
    monkeypatch.setattr(resources, "observed_descendant_rss_bytes", lambda ids: 1)
    signaled = []
    monkeypatch.setattr(
        resources.ProcessTree, "signal", lambda tree, sig: signaled.append(dict(tree.descendants))
    )
    monkeypatch.setattr(resources, "verify_owned_stopped", lambda tree: True)
    window = resources.WorkflowResourceWindow({}, threading.Event())
    window.start()
    table.update({72: (71, "later-unrelated"), 81: (root, "owned")})
    window.check(force=True)
    assert window._tracked == {81: "owned"}
    window.close()
    assert signaled and all(sample == {81: "owned"} for sample in signaled)


def test_prover_resource_failure_blocks_accepted_final_and_preserves_spent_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project(tmp_path)

    class ResourceFailingVerifier(Verifier):
        def final(self, files: list[Path]) -> dict[str, Any]:
            runtime.workflow_resources.failure = resources.WorkflowResourceFailure(
                "Shared budget exceeded during final check", code="workflow_memory_limit"
            )
            runtime.cancelled.set()
            return {"accepted": True}

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="standard", agent_calls=1),
        session=session,
        verifier=ResourceFailingVerifier(),
    )
    result = runtime.run()
    assert result["status"] == "resource_error"
    assert result["stop_code"] == "workflow_memory_limit"
    assert result["metrics"]["api_calls"] == 1
    assert result["workflow_resources"]["cleanup_verified"] is True
    persisted = json.loads((runtime.store.directory / "state.json").read_text())
    assert persisted["status"] == "resource_error"


def test_missing_controller_identity_refuses_before_actor_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources, "_processes", lambda: {})
    window = resources.WorkflowResourceWindow({}, threading.Event())
    with pytest.raises(resources.WorkflowResourceFailure):
        window.start()
    window.close()


def test_sampling_helpers_are_pruned_after_exact_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    root = os.getpid()
    table = {root: (1, "controller")}
    monkeypatch.setattr(resources, "_processes", lambda: table.copy())
    monkeypatch.setattr(resources, "observed_descendant_rss_bytes", lambda ids: 1)
    monkeypatch.setattr(resources.ProcessTree, "signal", lambda tree, sig: None)
    monkeypatch.setattr(resources, "verify_owned_stopped", lambda tree: True)
    window = resources.WorkflowResourceWindow({}, threading.Event())
    window.start()
    for batch in range(10):
        table = {root: (1, "controller"), 900 + batch: (root, f"helper-{batch}")}
        window.check(force=True)
        assert len(window._tracked) == 1
    table = {root: (1, "controller")}
    window.check(force=True)
    assert not window._tracked
    window.close()


def test_existing_checker_snapshot_preserves_cached_identity_and_workspace_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import check_process

    process = SimpleNamespace(pid=71, returncode=None)
    tree = ProcessTree(71, {72: "child"}, process=process)
    tree.root_identity = "original-root"
    worker = SimpleNamespace(closed=False, process=process, tree=tree)
    monkeypatch.setattr(
        check_process,
        "_WORKERS",
        {(tmp_path, tmp_path / "owned" / "checks"): worker, (tmp_path, tmp_path / "other"): worker},
    )
    assert check_process.owned_checker_identities(tmp_path / "owned") == {
        71: "original-root",
        72: "child",
    }
    tree.root_identity = None
    with pytest.raises(NativeCleanupUnverified, match="identity is unknown"):
        check_process.owned_checker_identities(tmp_path / "owned")
    assert process.returncode is None
