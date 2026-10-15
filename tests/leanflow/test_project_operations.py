"""Keep project setup mutually exclusive with new and already-running workflows."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.runtime import project_operations as operations


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_setup_excludes_workflows_and_other_setup_without_project_writes(tmp_path):
    root = tmp_path / "new-project"
    with operations.project_setup(root):
        for operation in (operations.project_setup, operations.project_activity):
            with pytest.raises(operations.ProjectBusyError, match="setup"):
                with operation(root):
                    pytest.fail("Conflicting operation was admitted")
    assert not root.exists()
    with operations.project_activity(root):
        pass


def test_multiple_workflows_share_ownership_but_setup_requires_all_to_finish(tmp_path):
    root = tmp_path / "project"
    with operations.project_activity(root):
        with operations.project_activity(root):
            with pytest.raises(operations.ProjectBusyError, match="running"):
                with operations.project_setup(root):
                    pytest.fail("Setup raced a workflow")
        with pytest.raises(operations.ProjectBusyError, match="running"):
            with operations.project_setup(root):
                pytest.fail("A failed lock attempt released another owner's lock")
    with operations.project_setup(root):
        pass


def test_canonical_project_paths_share_one_lock(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with operations.project_activity(root):
        with pytest.raises(operations.ProjectBusyError):
            with operations.project_setup(alias):
                pytest.fail("A symlink bypassed project ownership")


@pytest.mark.parametrize(
    "relative",
    [
        ".leanflow/workflow-state/prover/controller.lock",
        ".leanflow/native-workflows/old-run/controller.lock",
    ],
)
def test_setup_refuses_existing_controller_and_preserves_every_file(tmp_path, relative):
    root = tmp_path / "project"
    lock = root / relative
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"existing controller evidence")
    (root / "Main.lean").write_text("theorem pending : True := by sorry\n")
    before = _files(root)
    with lock.open("r") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(operations.ProjectBusyError, match="running"):
            with operations.project_setup(root):
                pytest.fail("An old controller was not protected")
    assert _files(root) == before
    with operations.project_setup(root):
        with lock.open("r") as late_owner:
            with pytest.raises(BlockingIOError):
                fcntl.flock(late_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert _files(root) == before


def test_legacy_live_pid_blocks_setup_without_mutating_state(tmp_path):
    root = tmp_path / "project"
    status = root / ".leanflow/workflow-state/live_status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"process_id": os.getpid(), "phase": "agent", "terminal": False}))
    before = _files(root)
    with pytest.raises(operations.ProjectBusyError, match="running"):
        with operations.project_setup(root):
            pytest.fail("A pre-migration workflow was not protected")
    assert _files(root) == before


def test_legacy_run_metadata_protects_owner_after_live_pointer_moves(tmp_path):
    root = tmp_path / "project"
    metadata = root / ".leanflow/workflow-state/activity/run-metadata/older-run.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"process_id": os.getpid(), "project_root": str(root)}))
    with pytest.raises(operations.ProjectBusyError, match="running"):
        with operations.project_setup(root):
            pytest.fail("Run metadata owner was missed")


def test_unreadable_ownership_fails_closed_and_corrected_status_can_retry(tmp_path):
    root = tmp_path / "project"
    status = root / ".leanflow/workflow-state/live_status.json"
    status.parent.mkdir(parents=True)
    status.write_text("{incomplete")
    before = _files(root)
    with pytest.raises(operations.ProjectBusyError, match="Cannot verify"):
        with operations.project_setup(root):
            pytest.fail("Unreadable ownership was ignored")
    assert _files(root) == before
    status.write_text(json.dumps({"terminal": True}))
    with operations.project_setup(root):
        pass


def test_verified_identity_does_not_mistake_reused_pid_for_active_owner(tmp_path, monkeypatch):
    root = tmp_path / "project"
    status = root / ".leanflow/workflow-state/live_status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"process_id": os.getpid(), "process_token_sha256": "old-token"}))
    monkeypatch.setattr(operations, "process_identity_matches", lambda identity: False)
    with operations.project_setup(root):
        pass


@pytest.mark.parametrize("terminal", [True, False])
def test_finished_or_dead_legacy_owner_does_not_block_retry(tmp_path, monkeypatch, terminal):
    root = tmp_path / "project"
    status = root / ".leanflow/workflow-state/live_status.json"
    status.parent.mkdir(parents=True)
    status.write_text(
        json.dumps({"process_id": os.getpid(), "phase": "agent", "terminal": terminal})
    )
    if not terminal:
        monkeypatch.setattr(operations, "_process_is_live", lambda payload: False)
    with operations.project_setup(root):
        pass


def test_setup_and_workflow_exclusion_is_cross_process(tmp_path):
    root = tmp_path / "project"
    code = (
        "import sys; from pathlib import Path; "
        "from leanflow_cli.runtime.project_operations import project_activity; "
        "context = project_activity(Path(sys.argv[1])); context.__enter__(); "
        "print('ready', flush=True); sys.stdin.readline(); context.__exit__(None, None, None)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(operations.ProjectBusyError):
            with operations.project_setup(root):
                pytest.fail("Process-local ownership allowed setup")
        child.communicate("done\n", timeout=10)
        assert child.returncode == 0
        with operations.project_setup(root):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


@pytest.mark.parametrize("setup_already_running", [False, True])
def test_formalization_intake_shares_project_ownership(
    tmp_path, monkeypatch, setup_already_running
):
    from leanflow_cli import workflow

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(
        workflow, "discover_leanflow_project", lambda cwd: SimpleNamespace(root=root, label="Demo")
    )
    monkeypatch.setattr(
        workflow, "resolve_runtime_provider", lambda **kwargs: {"provider": "codex"}
    )

    def intake(**kwargs):
        assert not setup_already_running, "Formalization wrote into a setup-owned project"
        with pytest.raises(operations.ProjectBusyError):
            with operations.project_setup(root):
                pytest.fail("Setup raced formalization intake")
        raise ValueError("intake reached safely")

    monkeypatch.setattr(workflow, "prepare_formalization_document_context", intake)
    monkeypatch.setattr(
        "leanflow_cli.formalization.preparation_launch.prepare_formalization_launch",
        lambda *args, **kwargs: {
            "root": root,
            "budget_path": root / ".leanflow/formalization-campaigns/fixture/budget.json",
        },
    )
    if setup_already_running:
        with operations.project_setup(root):
            with pytest.raises(operations.ProjectBusyError):
                workflow.resolve_workflow_request("/formalize paper.tex", active_cwd=root)
    else:
        with pytest.raises(ValueError, match="intake reached safely"):
            workflow.resolve_workflow_request("/formalize paper.tex", active_cwd=root)
    assert not (root / ".leanflow").exists()
