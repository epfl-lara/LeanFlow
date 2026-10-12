"""Preserve requested imports through independent checking and proof installation."""

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.job_controller import candidate_feedback
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from tests.leanflow.test_prover_runtime import Verifier, project


class ImportVerifier(Verifier):
    """Require the requested import and record all protected-type gates."""

    def __init__(self, *, signatures_match: bool = True) -> None:
        super().__init__()
        self.signatures_match = signatures_match
        self.signature_checks = 0

    def capture_signatures(self, dag, documents, workspace, *, initialize=True):
        self.signature_checks += 1
        assert not initialize
        assert "import Mathlib.Extra\n" in documents["Main.lean"].render()
        return {"accepted": self.signatures_match, "error": "protected type changed"}

    def check(self, node, file, **kwargs):
        self.checked.append(node.id)
        return {"accepted": "import Mathlib.Extra\n" in file.read_text()}


@pytest.mark.parametrize("source_kind", ["explicit", "scratch"])
def test_import_request_is_checked_without_mutating_source_then_installed(
    tmp_path: Path, source_kind: str
) -> None:
    path = project(tmp_path)
    original = path.read_text()
    verifier = ImportVerifier()
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        verifier=verifier,
        session=lambda **_: {},
    )
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    report: dict[str, Any] = {"proof": "trivial"}
    if source_kind == "explicit":
        report["imports"] = ["Mathlib.Extra"]
    else:
        Path(job["scratch_path"]).write_text("import Mathlib.Extra\n" + original)
    receipt = candidate_feedback(runtime, job, json.dumps(report))
    assert receipt["accepted"], receipt
    assert path.read_text() == original, "Submission feedback must not publish imports."
    runtime._handle_result(
        job, {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}
    )
    assert node.status == "proved"
    assert path.read_text() == "import Mathlib.Extra\n" + original.replace("sorry", "trivial")
    assert verifier.signature_checks >= 1


def test_imports_cannot_change_protected_types(tmp_path: Path) -> None:
    path = project(tmp_path)
    original = path.read_text()
    verifier = ImportVerifier(signatures_match=False)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        verifier=verifier,
        session=lambda **_: {},
    )
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    receipt = candidate_feedback(
        runtime, job, json.dumps({"proof": "trivial", "imports": ["Mathlib.Extra"]})
    )
    assert not receipt["accepted"]
    assert "protected type" in receipt["error"]
    assert not verifier.checked
    assert path.read_text() == original


def test_resume_rechecks_saved_import_candidate_without_another_model_call(tmp_path: Path) -> None:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        verifier=ImportVerifier(),
        session=lambda **_: {},
    )
    node = runtime.dag.nodes[0]
    node.signature_sha256 = "pinned"
    job, _ = runtime._new_job("prover", node=node)
    Path(job["scratch_path"]).write_text("import Mathlib.Extra\n" + path.read_text())
    (Path(job["workspace"]) / "candidate.txt").write_text("trivial")
    runtime._finish_job(job, {"status": "completed", "api_calls": 1, "final_response": ""})
    node.status = "blocked"
    job["result_processed"] = True
    runtime.state["design_plan_complete"] = True
    runtime.state.update(status="budget_exhausted", phase="budget_exhausted", terminal=True)
    runtime._persist()

    def forbidden(**kwargs):
        pytest.fail("Saved candidate must be independently checked before new model work.")

    resumed = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        run_id=runtime.run_id,
        resume=True,
        verifier=ImportVerifier(),
        session=forbidden,
    )
    state = resumed.run()
    assert state["status"] == "completed", state.get("error")
    assert state["metrics"]["agent_calls"] == 1
    assert path.read_text().startswith("import Mathlib.Extra\n")
    assert "sorry" not in path.read_text()


def test_failed_proof_integration_rolls_back_its_imports_together(tmp_path: Path) -> None:
    path = project(tmp_path)
    original = path.read_text()
    verifier = ImportVerifier()
    verifier.compile_module = lambda relative: {"accepted": False}
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        verifier=verifier,
        session=lambda **_: {},
    )
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    report = {"proof": "trivial", "imports": ["Mathlib.Extra"]}
    assert candidate_feedback(runtime, job, json.dumps(report))["accepted"]
    from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

    with pytest.raises(InfrastructureFailure):
        runtime._handle_result(
            job, {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}
        )
    assert path.read_text() == original
    assert not runtime.documents["Main.lean"].proof_imports
    assert node.candidate_imports == ["Mathlib.Extra"]


@pytest.mark.parametrize(
    "imports", ["Mathlib.Extra", ["Mathlib.Extra\naxiom bad : False"], ["../Other"], [None]]
)
def test_invalid_import_request_returns_feedback_in_the_same_session(
    tmp_path: Path, imports
) -> None:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        verifier=Verifier(),
        session=lambda **_: {},
    )
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    receipt = candidate_feedback(runtime, job, json.dumps({"proof": "trivial", "imports": imports}))
    assert not receipt["accepted"]
    assert "import" in receipt["error"].lower()
    assert not runtime.verifier.checked
