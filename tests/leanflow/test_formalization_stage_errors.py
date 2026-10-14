"""Classify source/setup drift as terminal re-preparation, preserving correction for author errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.runtime.harness import HarnessResult, IsolationPolicy
from leanflow_cli.formalization import preparation, proposal
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_directory,
    load_document,
    load_receipt,
    save_revision,
)
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.native import harness_formalization_stages, harness_workflow


@pytest.mark.parametrize("drift", ["source", "preparation"])
def test_submission_drift_requires_repreparation_in_exact_rejected_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    paper = tmp_path / "paper.tex"
    paper.write_text(r"\begin{theorem}True holds.\end{theorem}")
    record = create_document(tmp_path, paper)
    record.update(
        workflow_protocol=2,
        preparation_id="prepared",
        preparation_plan_id="plan",
        slice_source_ids=[item["id"] for item in record["inventory"]["items"]],
    )
    record = save_revision(document_directory(tmp_path, record["document_id"]), record)
    snapshot = tmp_path / ".leanflow/author"
    job = snapshot / ".leanflow-job"
    job.mkdir(parents=True)
    (job / "formalization-proposal.json").write_text(json.dumps({"phase": "representative_slice"}))
    if drift == "source":
        paper.write_text(r"\begin{theorem}False holds.\end{theorem}")
    else:
        monkeypatch.setattr(
            preparation,
            "assert_preparation_current",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError("Prepared project source changed; estimate and check setup again")
            ),
        )
    result = proposal.check_proposal(
        root=tmp_path,
        snapshot=snapshot,
        baseline={},
        directory=tmp_path / ".leanflow/checks",
        document_id=record["document_id"],
        base_revision_id=record["revision_id"],
        selection_token=current_selection_token(tmp_path, record["document_id"]),
        allowed_axioms=(),
    )
    assert not result["accepted"]
    assert result["status"] == "check_error" and result["error_code"] == "needs_reprepare"
    selected = load_document(tmp_path, record["document_id"])
    assert selected["revision_id"] == result["revision_id"]
    receipt = load_receipt(selected)
    assert receipt and receipt["status"] == "check_error"
    assert receipt["checks"]["error_code"] == "needs_reprepare" and receipt["handoff"] is None


@pytest.mark.parametrize("origin", ["proposal", "assignment"])
def test_native_submission_drift_stops_author_and_preserves_terminal_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    paper = tmp_path / "paper.tex"
    paper.write_text(r"\begin{theorem}True holds.\end{theorem}")
    (tmp_path / "Main.lean").write_text("import Init\n")
    record = create_document(tmp_path, paper)
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.preflight_dependencies", lambda *args: {}
    )
    monkeypatch.setattr(
        "leanflow_cli.native.harness_context.load_skill", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment._toolchain_roots", lambda root: []
    )
    monkeypatch.setattr(
        harness_workflow,
        "build_runtime_isolation",
        lambda backend, cwd, **kwargs: IsolationPolicy(cwd),
    )
    stages, checks, authors, bridges = [], [], [], []
    bridge_class = harness_workflow.NativeBridge

    def bridge(**kwargs):
        selected = bridge_class(**kwargs)
        bridges.append(selected)
        return selected

    monkeypatch.setattr(harness_workflow, "NativeBridge", bridge)

    def assignment(root, environment, context, prompt):
        stages.append(prompt)
        if origin == "assignment" and len(stages) > 1:
            raise RepreparationRequired("Prepared source changed during submission")
        return context, prompt

    monkeypatch.setattr(harness_formalization_stages, "stage_assignment", assignment)

    def check(**kwargs):
        checks.append(kwargs)
        return {
            "accepted": False,
            "status": "check_error" if origin == "proposal" else "check_rejected",
            "error_code": "needs_reprepare" if origin == "proposal" else "invalid_translation",
            "error": "Prepared source changed during submission",
        }

    monkeypatch.setattr(proposal, "check_proposal", check)

    def invoke(request, on_event=None):
        authors.append(request)
        request.on_started()
        assert not request.cancelled()
        feedback = bridges[0].callbacks["submit_files"]("Submitted translation")
        assert feedback["error_code"] == "needs_reprepare"
        assert request.cancelled()
        assert bridges[0].callbacks["submit_files"]("Retry the same translation") == feedback
        return HarnessResult(
            "cancelled", "Stopped after source drift", "author-session", {"input_tokens": 1}
        )

    monkeypatch.setattr(harness_workflow, "run_harness", invoke)
    result = harness_workflow.run_workflow(
        {
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            "LEANFLOW_NATIVE_PROVIDER": "claude",
            "LEANFLOW_WORKFLOW_RUN_ID": "terminal-fixture",
            "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
            "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
            "LEANFLOW_PROVER_AGENT_CALLS": "3",
        }
    )
    assert result["status"] == "needs_reprepare" and "source changed" in result["error"]
    assert len(authors) == len(checks) == 1
    directory = tmp_path / ".leanflow/native-workflows/terminal-fixture"
    assert json.loads((directory / "state.json").read_text())["status"] == "needs_reprepare"
    assert len(json.loads((directory / "limits.json").read_text())["invocations"]) == 1
