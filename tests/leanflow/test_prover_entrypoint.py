"""Verify startup outcomes, safe resume lineage, and compatibility launch wrappers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.workflows.prover import entrypoint, runtime
from leanflow_cli.workflows.prover.config import ProverConfig


@pytest.fixture
def launch(tmp_path, monkeypatch):
    source = tmp_path / "Main.lean"
    source.write_text("theorem goal : True := by sorry\n")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "execution-new")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Main.lean")
    monkeypatch.delenv("LEANFLOW_PROVER_RESUME_RUN_ID", raising=False)
    observed = []

    class Observer:
        def __init__(self, root, run_id):
            self.run_id = run_id

        def start(self, state, **kwargs):
            observed.append(("start", self.run_id, dict(state)))

        def finish(self, state):
            observed.append(("finish", self.run_id, dict(state)))

        def event(self, *args):
            pass

        def publish(self, *args):
            pass

    monkeypatch.setattr(entrypoint, "RunObserver", Observer)
    return tmp_path, observed


def _state(root: Path, run_id="execution-new"):
    return json.loads(
        (root / ".leanflow/workflow-state/prover" / run_id / "state.json").read_text()
    )


def test_discovery_failure_publishes_exact_terminal_execution(launch):
    root, observed = launch
    source = root / "Main.lean"
    original = "def unfinished : Nat := sorry\n"
    source.write_text(original)
    assert entrypoint.main() == 1
    state = _state(root)
    assert state["terminal"] and state["startup_failed"] and state["status"] == "error"
    assert "fill_definitions" in state["error"]
    assert [(item[0], item[1]) for item in observed] == [
        ("start", "execution-new"),
        ("finish", "execution-new"),
    ]
    assert source.read_text() == original


def test_invalid_configuration_also_publishes_terminal_state(launch, monkeypatch):
    root, observed = launch
    monkeypatch.setenv("LEANFLOW_PROVER_JOB_API_CALLS", "0")
    assert entrypoint.main() == 1
    assert "job_api_calls" in _state(root)["error"]
    assert observed[-1][0] == "finish"


def test_missing_resume_snapshot_publishes_new_execution_failure(launch, monkeypatch):
    root, observed = launch
    monkeypatch.setenv("LEANFLOW_PROVER_RESUME_RUN_ID", "missing-old")
    assert entrypoint.main() == 1
    state = _state(root)
    assert state["parent_run_id"] == "missing-old"
    assert state["terminal"] and "resume source" in state["error"]
    assert observed[-1][1] == "execution-new"


def test_resume_source_conflict_preserves_old_snapshot_and_user_edits(launch, monkeypatch):
    root, _ = launch
    runtime.ProverRuntime(
        root=root,
        targets=[root / "Main.lean"],
        config=ProverConfig(),
        run_id="execution-old",
    )
    old = root / ".leanflow/workflow-state/prover/execution-old"
    before = {name: (old / name).read_bytes() for name in ("state.json", "source.json")}
    changed = "-- user edit\ntheorem goal : True := by sorry\n"
    (root / "Main.lean").write_text(changed)
    monkeypatch.setenv("LEANFLOW_PROVER_RESUME_RUN_ID", "execution-old")
    assert entrypoint.main() == 2
    state = _state(root)
    assert state["status"] == "source_conflict" and "protected source" in state["error"]
    assert "original snapshot was preserved" in state["next_step"]
    assert (root / "Main.lean").read_text() == changed
    assert {name: (old / name).read_bytes() for name in before} == before


def test_clone_relocates_paths_without_rewriting_proof_or_source_text(launch):
    root, _ = launch
    old = root / ".leanflow/workflow-state/prover/old"
    old.mkdir(parents=True)
    literal = str(old / "a-proof-string")
    (old / "state.json").write_text(
        json.dumps(
            {
                "run_id": "old",
                "jobs": [{"workspace": str(old / "jobs/job1"), "notes": literal}],
                "metrics": {"api_calls": 37},
            }
        )
    )
    original = json.dumps({"Main.lean": {"baseline": literal, "path": "Main.lean"}})
    (old / "source.json").write_text(original)
    entrypoint.clone_resume_run(root, "old", "new")
    state = _state(root, "new")
    assert state["jobs"][0]["workspace"].endswith("/new/jobs/job1")
    assert state["jobs"][0]["notes"] == literal
    assert state["metrics"]["api_calls"] == 37
    assert state["parent_run_id"] == "old"
    assert (
        json.loads((old.parent / "new/source.json").read_text())["Main.lean"]["baseline"] == literal
    )
    assert (old / "source.json").read_text() == original


def test_lock_conflict_does_not_steal_existing_live_observer(launch, monkeypatch):
    root, observed = launch

    def busy(*args):
        raise BlockingIOError("owned")

    monkeypatch.setattr(entrypoint.fcntl, "flock", busy)
    assert entrypoint.main() == 1
    assert _state(root)["terminal"] and not observed
    assert _state(root)["status"] == "environment_error"


def test_runtime_main_preserves_module_launch_compatibility(monkeypatch):
    monkeypatch.setattr(entrypoint, "main", lambda: 47)
    assert runtime.main() == 47


@pytest.mark.parametrize("explicit", [False, True])
def test_resume_keeps_original_target_scope(launch, monkeypatch, explicit):
    root, _ = launch
    runtime.ProverRuntime(
        root=root, targets=[root / "Main.lean"], config=ProverConfig(), run_id="execution-old"
    )
    (root / "Other.lean").write_text("theorem other : True := by sorry\n")
    monkeypatch.setenv("LEANFLOW_PROVER_RESUME_RUN_ID", "execution-old")
    if explicit:
        monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Other.lean")
    else:
        monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE")
    received = []
    original = runtime.ProverRuntime

    def construct(**kwargs):
        received.extend(kwargs["targets"])
        instance = original(**kwargs)
        instance.run = lambda: {"status": "completed", "metrics": {}}
        return instance

    monkeypatch.setattr(runtime, "ProverRuntime", construct)
    code = entrypoint.main()
    if explicit:
        assert code == 1
        assert not received
        assert "target scope" in _state(root)["error"]
    else:
        assert code == 0
        assert received == [root / "Main.lean"]
