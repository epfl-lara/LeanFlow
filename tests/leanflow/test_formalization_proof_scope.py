"""Reject handoff setup mutations before checks, helper registration, or source writes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from leanflow_cli.workflows.prover import planning_controller
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.job_controller import candidate_feedback
from leanflow_cli.workflows.prover.role_limits import recovery_actions
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.source import sorry_spans
from tests.leanflow.test_prover_runtime import Verifier, project


def _handoff(path: Path) -> dict:
    return {
        "revision_id": "rev-fixture",
        "acceptance_policy": {"allowed_axioms": list(ProverConfig().allowed_axioms)},
        "obligations": [
            {
                "id": "target",
                "file": "Main.lean",
                "module": "Main",
                "declaration": "goal",
                "source_name": "goal",
                "holes": [0],
                "hole_spans": [list(sorry_spans(path.read_text())[0])],
                "source_ids": ["claim"],
                "depends_on": [],
            }
        ],
    }


def _runtime(tmp_path: Path) -> tuple[ProverRuntime, Path]:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="research"),
        verifier=Verifier(),
        session=lambda **kwargs: pytest.fail("Forbidden setup strategy must not start a model"),
        formalization_handoff=_handoff(path),
    )
    return runtime, path


@pytest.mark.parametrize("source_kind", ["explicit", "scratch", "retained"])
def test_new_proof_imports_rejected_before_check_or_canonical_write(
    tmp_path: Path, source_kind: str
) -> None:
    runtime, path = _runtime(tmp_path)
    before = path.read_bytes()
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    report = {"proof": "trivial"}
    if source_kind == "explicit":
        report["imports"] = ["Std.Extra"]
    elif source_kind == "scratch":
        Path(job["scratch_path"]).write_text("import Std.Extra\n" + path.read_text())
    else:
        node.candidate_imports = ["Std.Extra"]
        assert not runtime._accept(node, ["trivial"], job["id"])
    if source_kind != "retained":
        receipt = candidate_feedback(runtime, job, json.dumps(report))
        assert receipt["accepted"] is False and "formalization" in receipt["error"]
    assert not runtime.verifier.checked
    assert path.read_bytes() == before
    assert not runtime.documents["Main.lean"].proof_imports


def test_existing_import_requests_still_allow_exact_proof_slots(tmp_path: Path) -> None:
    runtime, path = _runtime(tmp_path)
    before = path.read_text()
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    receipt = candidate_feedback(runtime, job, json.dumps({"proof": "trivial", "imports": ["Std"]}))
    assert receipt["accepted"], receipt
    assert runtime._accept(node, ["trivial"], job["id"])
    assert path.read_text() == before.replace("sorry", "trivial")


def test_handoff_disables_decomposition_before_planning_or_helper_registration(
    tmp_path: Path,
) -> None:
    runtime, path = _runtime(tmp_path)
    before = path.read_bytes()
    configuration = (tmp_path / "lakefile.toml").read_bytes()
    document = copy.deepcopy(runtime.documents["Main.lean"].to_dict())
    assert recovery_actions(runtime)["decompose"]["can_start"] is False
    assert runtime._context(runtime.dag.nodes[0])["formalization_proof_scope"]
    assert runtime._research_plan("Split the source claim into new helpers") is False
    assert runtime.state["design_plan_complete"]
    assert not runtime.state["jobs"]
    with pytest.raises(ValueError, match="helper materialization"):
        planning_controller.materialize(
            runtime, runtime.dag, {"helper": "theorem helper : True := by sorry"}
        )
    with pytest.raises(ValueError, match="library installation"):
        planning_controller.install_planned_libraries(runtime, [{"name": "Extra"}])
    assert path.read_bytes() == before
    assert (tmp_path / "lakefile.toml").read_bytes() == configuration
    assert runtime.documents["Main.lean"].to_dict() == document
    assert not (tmp_path / "LeanflowHelpers").exists()


def test_handoff_context_exposes_frozen_guidance_references_without_mutable_aliases(
    tmp_path: Path,
) -> None:
    runtime, _ = _runtime(tmp_path)
    assert runtime.formalization_handoff is not None
    runtime.formalization_handoff.update(
        guidance_refs=[{"file": "paper.tex", "source_id": "claim"}],
        definition_refs=[{"name": "Example.definition", "type": "Nat"}],
    )
    context = runtime._context(runtime.dag.nodes[0])
    assert context["formalization_guidance_refs"] == runtime.formalization_handoff["guidance_refs"]
    assert (
        context["formalization_definition_refs"] == runtime.formalization_handoff["definition_refs"]
    )
    context["formalization_guidance_refs"][0]["source_id"] = "changed"
    assert runtime.formalization_handoff["guidance_refs"][0]["source_id"] == "claim"


def test_axiom_policy_override_rejected_before_runtime_is_created(tmp_path: Path) -> None:
    path = project(tmp_path)
    config = ProverConfig(allowed_axioms=(*ProverConfig().allowed_axioms, "Lean.ofReduceBool"))
    with pytest.raises(ValueError, match="axiom policy"):
        ProverRuntime(
            root=tmp_path,
            targets=[path],
            config=config,
            verifier=Verifier(),
            formalization_handoff=_handoff(path),
        )
    assert not (tmp_path / ".leanflow/workflow-state/prover").exists()


@pytest.mark.parametrize("changed", ["handoff", "setup", "materialization"])
def test_resume_rejects_out_of_scope_setup_before_transaction_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    runtime, path = _runtime(tmp_path)
    before = path.read_bytes()
    if changed == "handoff":
        runtime.state["formalization_handoff"]["revision_id"] = "different-revision"
    elif changed == "setup":
        runtime.documents["Main.lean"].proof_imports = ["Std.Extra"]
    else:
        (runtime.store.directory / "source-transaction.json").write_text(
            json.dumps({"kind": "materialization"})
        )
    runtime._persist()
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.source_transaction.recover_proof",
        lambda *_: pytest.fail("Unsupported saved state must not recover canonical writes"),
    )
    with pytest.raises(ValueError, match="(?:scope|handoff)"):
        ProverRuntime(
            root=tmp_path,
            targets=[path],
            config=ProverConfig(mode="research"),
            run_id=runtime.run_id,
            resume=True,
            verifier=Verifier(),
            formalization_handoff=_handoff(path),
        )
    assert path.read_bytes() == before
