"""Keep dependent helper staging, source protection, and recovery consistent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import planning_controller
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.models import Dag
from leanflow_cli.workflows.prover.planning import apply_proposal
from leanflow_cli.workflows.prover.runtime import (
    BudgetExhausted,
    InfrastructureFailure,
    ProverRuntime,
)
from leanflow_cli.workflows.prover.scheduler import ready_nodes
from leanflow_cli.workflows.prover.source import SourceDocument


class RecordingVerifier:
    """Record verification boundaries without invoking a provider or Lean."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.compiled: list[str] = []
        self.signature_checks = 0

    def compile_module(self, relative: str) -> dict[str, Any]:
        self.compiled.append(relative)
        artifact = self.root / ".lake/build/lib/lean" / Path(relative).with_suffix(".olean")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("mock artifact for " + (self.root / relative).read_text())
        return {"accepted": True}

    def capture_signatures(
        self, dag: Dag, documents: dict[str, SourceDocument], directory: Path, **kwargs: Any
    ) -> dict[str, Any]:
        self.signature_checks += 1
        return {"accepted": True}


def make_runtime(root: Path, **kwargs: Any) -> ProverRuntime:
    """Create a minimal research project with an immutable original source."""
    source = root / "Main.lean"
    if not source.exists():
        source.write_text("import Std\n\ntheorem goal : True := by sorry\n")
        (root / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    return ProverRuntime(
        root=root,
        targets=[source],
        config=ProverConfig(mode="research"),
        verifier=RecordingVerifier(root),
        **kwargs,
    )


def proposal_for(runtime: ProverRuntime) -> dict[str, Any]:
    """Build a root with a generated helper that itself imports another helper."""
    return {
        "plan": "Prove the leaf, composite, and root in dependency order.",
        "nodes": [
            {
                "id": "leaf",
                "name": "leaf",
                "statement": "theorem leaf : True ∧ True := by sorry",
                "file": "LeanFlowProofs/Leaf.lean",
                "dependencies": [],
            },
            {
                "id": "composite",
                "name": "composite",
                "statement": "theorem composite : (True ∧ True) ∧ True := by sorry",
                "file": "LeanFlowProofs/Composite.lean",
                "dependencies": ["leaf"],
            },
            {"id": runtime.dag.roots[0], "dependencies": ["composite"]},
        ],
    }


def test_signature_timeout_preserves_paid_plan_instead_of_replanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["role"])
        report = {"accepted": True} if kwargs["role"] == "review" else proposal_for(runtime)
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}

    runtime = make_runtime(tmp_path, session=session)

    def signatures(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("initialize") is False:
            return {
                "accepted": False,
                "error": "kernel type inspection failed",
                "inspect": {"success": False, "error_code": "check_timeout", "timed_out": True},
            }
        return {"accepted": True}

    monkeypatch.setattr(runtime.verifier, "capture_signatures", signatures)
    result = runtime.run()
    assert result["status"] == "environment_error"
    assert calls == ["orchestrator", "orchestrator", "review"]
    assert result["metrics"]["api_calls"] == 3
    assert result["planning_request"]["steps"]["review-0"]


def test_dependent_helpers_stage_once_and_remain_consistent_on_resume(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    original = runtime.documents["Main.lean"].baseline
    dag, skeletons = apply_proposal(runtime.dag, proposal_for(runtime), max_nodes=10)

    planning_controller.materialize(runtime, dag, skeletons)

    assert runtime.verifier.compiled == [
        "LeanFlowProofs/Leaf.lean",
        "LeanFlowProofs/Composite.lean",
    ]
    assert runtime.verifier.signature_checks == 1
    runtime._assert_sources()
    assert runtime.documents["Main.lean"].baseline == original
    composite = runtime.documents["LeanFlowProofs/Composite.lean"]
    assert composite.render().count("import LeanFlowProofs.Leaf\n") == 1
    assert (tmp_path / composite.path).read_text() == composite.render()
    assert [n.id for n in ready_nodes(dag, order="bottom-up", active=set(), limit=4)] == ["leaf"]
    assert not (runtime.store.directory / "source-transaction.json").exists()

    restored = make_runtime(tmp_path, run_id=runtime.run_id, resume=True)
    restored._assert_sources()
    assert restored.dag.to_dict() == dag.to_dict()

    # Replanning can remove a dependency without leaving its import frozen into a baseline.
    revised, skeletons = apply_proposal(
        restored.dag, {"nodes": [{"id": "composite", "dependencies": []}]}, max_nodes=10
    )
    planning_controller.materialize(restored, revised, skeletons)
    restored._assert_sources()
    assert "import LeanFlowProofs.Leaf" not in (tmp_path / composite.path).read_text()
    assert not (tmp_path / "LeanFlowProofs/Leaf.lean").exists()


def test_real_edit_to_generated_helper_is_preserved_and_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(tmp_path)
    dag, skeletons = apply_proposal(runtime.dag, proposal_for(runtime), max_nodes=10)
    helper = tmp_path / "LeanFlowProofs/Composite.lean"

    def edit_after_checks(*args: Any, **kwargs: Any) -> dict[str, Any]:
        helper.write_text(helper.read_text() + "-- external correction\n")
        return {"accepted": True}

    monkeypatch.setattr(runtime.verifier, "capture_signatures", edit_after_checks)
    with pytest.raises(RuntimeError, match="protected source changed"):
        planning_controller.materialize(runtime, dag, skeletons)
    assert helper.read_text().endswith("-- external correction\n")
    assert (runtime.store.directory / "source-transaction.json").exists()


@pytest.mark.parametrize("conflict", ["document", "transaction"])
def test_source_consistency_failure_keeps_review_for_zero_call_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    roles: list[str] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        roles.append(kwargs["role"])
        result = {"accepted": True} if kwargs["role"] == "review" else proposal_for(runtime)
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(result)}

    runtime = make_runtime(tmp_path, session=session)
    materialize = planning_controller.materialize

    def inconsistent_source(*args: Any, **kwargs: Any) -> None:
        if conflict == "document":
            SourceDocument("Unexpected.lean", "-- expected\n").assert_current(tmp_path)
        else:
            from leanflow_cli.workflows.prover.source_transaction import (
                begin_materialization,
                materialized_write,
                recover_materialization,
            )

            journal = begin_materialization(runtime)
            try:
                materialized_write(
                    runtime, journal, tmp_path / "lakefile.toml", b"new", expected_before=b"stale"
                )
            finally:
                recover_materialization(runtime, journal)

    monkeypatch.setattr(planning_controller, "materialize", inconsistent_source)
    with pytest.raises(InfrastructureFailure, match="protected source changed") as failure:
        runtime._research_plan("Construct the dependency graph.")
    assert failure.value.status == "source_conflict"
    assert roles == ["orchestrator", "orchestrator", "review"]
    assert runtime.consumed == 3
    assert runtime.state["planning_request"]["steps"]["review-0"]

    monkeypatch.setattr(planning_controller, "materialize", materialize)
    assert runtime._research_plan("Continue the saved plan.")
    assert roles == ["orchestrator", "orchestrator", "review"]
    assert runtime.consumed == 3
    assert runtime.state["phase"] == "proving"
    runtime._assert_sources()


def test_interrupted_signature_gate_recovers_transaction_and_reuses_paid_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume the production interruption point without new planning calls or a new baseline."""
    roles: list[str] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        roles.append(kwargs["role"])
        result = {"accepted": True} if kwargs["role"] == "review" else proposal_for(runtime)
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(result)}

    runtime = make_runtime(tmp_path, session=session)
    before = (tmp_path / "Main.lean").read_bytes()

    def interrupt_staging(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("initialize") is False:
            raise KeyboardInterrupt
        return {"accepted": True}

    monkeypatch.setattr(runtime.verifier, "capture_signatures", interrupt_staging)
    stopped = runtime.run()
    assert stopped["status"] == "interrupted"
    assert stopped["metrics"]["api_calls"] == 3
    assert any(change.get("pending") for change in stopped["changes"])
    assert not any(change.get("status") == "rolled_back" for change in stopped["changes"])
    assert (runtime.store.directory / "source-transaction.json").exists()
    assert (tmp_path / "LeanFlowProofs/Composite.lean").exists()

    restored = make_runtime(tmp_path, session=session, run_id=runtime.run_id, resume=True)
    assert (tmp_path / "Main.lean").read_bytes() == before
    assert not (tmp_path / "LeanFlowProofs/Composite.lean").exists()
    assert restored.config == runtime.config
    assert restored.elapsed_before == stopped["metrics"]["elapsed_s"]
    assert restored.consumed == 3
    assert restored._research_plan("Resume the accepted split.")
    assert roles == ["orchestrator", "orchestrator", "review"]
    assert restored.consumed == 3
    restored._assert_sources()


def test_reversing_helper_dependency_drops_old_import_while_both_remain(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    dag, skeletons = apply_proposal(runtime.dag, proposal_for(runtime), max_nodes=10)
    planning_controller.materialize(runtime, dag, skeletons)
    reversed_dag, skeletons = apply_proposal(
        dag,
        {
            "nodes": [
                {"id": runtime.dag.roots[0], "dependencies": ["leaf"]},
                {"id": "leaf", "dependencies": ["composite"]},
                {"id": "composite", "dependencies": []},
            ]
        },
        max_nodes=10,
    )
    planning_controller.materialize(runtime, reversed_dag, skeletons)
    assert runtime.verifier.compiled[-2:] == [
        "LeanFlowProofs/Composite.lean",
        "LeanFlowProofs/Leaf.lean",
    ]
    assert "leaf" in runtime.dag.by_id() and "composite" in runtime.dag.by_id()
    assert (
        "import LeanFlowProofs.Leaf" not in (tmp_path / "LeanFlowProofs/Composite.lean").read_text()
    )
    assert (tmp_path / "LeanFlowProofs/Leaf.lean").read_text().count(
        "import LeanFlowProofs.Composite\n"
    ) == 1
    runtime._assert_sources()


def test_staging_deadline_is_not_reported_as_a_rejected_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def session(**kwargs: Any) -> dict[str, Any]:
        result = {"accepted": True} if kwargs["role"] == "review" else proposal_for(runtime)
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(result)}

    runtime = make_runtime(tmp_path, session=session)

    def expire(*args: Any, **kwargs: Any) -> None:
        runtime.started -= runtime.config.wall_time_s
        runtime._ensure_active()

    monkeypatch.setattr(planning_controller, "materialize", expire)
    with pytest.raises(BudgetExhausted):
        runtime._research_plan("Construct the dependency graph.")
    events = (runtime.store.directory / "events.jsonl").read_text()
    assert "plan_rejected" not in events
    assert runtime.consumed == 3


@pytest.mark.parametrize("missing_before", [False, True])
def test_failed_import_reversal_restores_artifacts_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_before: bool
) -> None:
    runtime = make_runtime(tmp_path)
    dag, skeletons = apply_proposal(runtime.dag, proposal_for(runtime), max_nodes=10)
    planning_controller.materialize(runtime, dag, skeletons)
    original = {name: (tmp_path / name).read_bytes() for name in runtime.documents}
    artifacts = tmp_path / ".lake/build/lib/lean/LeanFlowProofs"
    if missing_before:
        (artifacts / "Leaf.olean").unlink()
    original_artifacts = {path.name: path.read_bytes() for path in artifacts.glob("*.olean")}
    reversed_dag, skeletons = apply_proposal(
        dag,
        {
            "nodes": [
                {"id": dag.roots[0], "dependencies": ["leaf"]},
                {"id": "leaf", "dependencies": ["composite"]},
                {"id": "composite", "dependencies": []},
            ]
        },
        max_nodes=10,
    )
    monkeypatch.setattr(
        runtime.verifier, "capture_signatures", lambda *args, **kwargs: {"accepted": False}
    )
    with pytest.raises(ValueError, match="protected declaration type"):
        planning_controller.materialize(runtime, reversed_dag, skeletons)
    assert {name: (tmp_path / name).read_bytes() for name in original} == original
    assert {
        path.name: path.read_bytes() for path in artifacts.glob("*.olean")
    } == original_artifacts
    runtime._assert_sources()

    # Continuing with the previous graph keeps its old artifacts, rebuilding only missing caches.
    monkeypatch.setattr(
        runtime.verifier, "capture_signatures", lambda *args, **kwargs: {"accepted": True}
    )
    compiled_before = len(runtime.verifier.compiled)
    planning_controller.materialize(runtime, dag, {})
    assert len(runtime.verifier.compiled) == compiled_before + (2 if missing_before else 0)
