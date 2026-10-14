"""Require current per-item and whole-document review before controller publication."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.runtime.harness import BackendError, HarnessResult, IsolationPolicy
from leanflow_cli.native import harness_workflow as workflow
from leanflow_cli.workflows.prover.lean_environment import lean_environment_generation


@pytest.fixture
def formalization_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise the controller and strict reviewer with deterministic native/kernel seams."""
    from leanflow_cli.formalization import proposal as candidate
    from leanflow_cli.formalization import publish
    from leanflow_cli.formalization import review as strict_review
    from leanflow_cli.formalization.evidence import put_blob
    from leanflow_cli.formalization.records import create_document

    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.preflight_dependencies", lambda *args: {}
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment._toolchain_roots", lambda project: []
    )
    monkeypatch.delenv("LEAN_PATH", raising=False)
    source = tmp_path / "Main.lean"
    original = "-- Existing project\n"
    source.write_text(original)
    paper = tmp_path / "paper.tex"
    paper.write_text(r"\begin{theorem}Source theorem 1 states True.\end{theorem}")
    record = create_document(tmp_path, paper)
    item = record["inventory"]["items"][0]
    record.update(
        layout={"library": "Main"},
        environment_id="env",
        units=[
            {
                "id": "unit-t",
                "required_source_ids": [item["id"]],
                "declarations": [{"file": "Main.lean", "name": "source_statement"}],
                "representation_note": "Same assertion",
                "depends_on_units": [],
            }
        ],
        checks={
            "accepted": True,
            "declarations": [{"name": "source_statement", "pending": True}],
            "obligations": [],
        },
    )
    requests, checks = [], []
    bridges = []
    native_bridge = workflow.NativeBridge

    def bridge_factory(**kwargs):
        bridge = native_bridge(**kwargs)
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr(workflow, "NativeBridge", bridge_factory)
    monkeypatch.setattr(
        workflow, "build_runtime_isolation", lambda backend, cwd, **kw: IsolationPolicy(cwd)
    )
    monkeypatch.setattr(
        strict_review, "build_runtime_isolation", lambda backend, cwd, **kw: IsolationPolicy(cwd)
    )

    def verify(**kwargs):
        assert source.read_text() == original
        files = {"Main.lean": (kwargs["snapshot"] / "Main.lean").read_text()}
        checks.append(files)
        if "False" in files["Main.lean"]:
            return {"accepted": False, "error": "Changed statement rejected"}
        record["lean_files"] = {
            name: put_blob(Path(record["_directory"]) / "blobs", text.encode())
            for name, text in files.items()
        }
        return {
            "accepted": True,
            "revision_id": record["revision_id"],
            "record": record,
            "files": files,
            "environment_generation": lean_environment_generation(tmp_path),
        }

    def promote(root, baseline, metadata, checked, reviewed, directory, **kwargs):
        assert reviewed["accepted"] and len(reviewed["reviews"]) == 2
        for name, text in checked["files"].items():
            (root / name).write_text(text)
        return {"status": "published"}

    monkeypatch.setattr(candidate, "check_proposal", verify)
    monkeypatch.setattr(publish, "publish_checked", promote)

    def invoke(
        *,
        decision="PASS",
        calls=3,
        startup_error=False,
        submit_then_change=False,
        terminal_submission=False,
    ):
        def native(request, on_event=None):
            requests.append(request)
            assert source.read_text() == original
            if not request.tools and startup_error:
                raise BackendError("Reviewer authentication unavailable")
            request.on_started()
            if request.tools:
                (request.cwd / "Main.lean").write_text(
                    "theorem source_statement : True := by sorry\n"
                )
                if submit_then_change or terminal_submission:
                    bridges[-1].callbacks["submit_files"]("candidate")
                    if terminal_submission:
                        assert request.cancelled()
                        return HarnessResult(
                            "cancelled",
                            "Controller interrupted stale preparation",
                            "draft-session",
                            {"input_tokens": 5, "output_tokens": 5},
                        )
                    (request.cwd / "Main.lean").write_text(
                        "theorem source_statement : False := by sorry\n"
                    )
                return HarnessResult(
                    "completed",
                    "Draft ready.",
                    "draft-session",
                    {"input_tokens": 5, "output_tokens": 5},
                )
            assert checks
            assignment = json.loads(request.prompt[len(strict_review._CONTRACT) + 1 :])
            dossier = assignment["dossier"]
            assert "Source theorem 1 states True" in request.prompt
            response = {
                "revision_id": assignment["revision_id"],
                "dossier_id": assignment["dossier_id"],
                "verdicts": [
                    {
                        "unit_id": u["id"],
                        "source_ids": u["required_source_ids"],
                        "decision": decision,
                        "rationale": "Checked source qualifiers",
                        "issues": [] if decision == "PASS" else ["Restore positivity assumption"],
                    }
                    for u in dossier["units"]
                ],
                "coverage": [
                    {
                        "region_id": r["id"],
                        "decision": decision,
                        "rationale": "Audited whole region",
                    }
                    for r in dossier["regions"]
                ],
                "claims": [
                    {
                        "source_id": source_id,
                        "status": "asserted",
                        "rationale": "Verified source content asserts this claim.",
                    }
                    for source_id in sorted(
                        {
                            key
                            for unit in assignment["dossier"]["units"]
                            for key in unit["required_source_ids"]
                        }
                    )
                ],
                "coherence_decision": decision,
                "feedback": (
                    "Source verified" if decision == "PASS" else "Restore positivity assumption"
                ),
            }
            return HarnessResult(
                "completed",
                json.dumps(response),
                "review-session",
                {"input_tokens": 7, "output_tokens": 3},
            )

        monkeypatch.setattr(workflow, "run_harness", native)
        monkeypatch.setattr(strict_review, "run_harness", native)
        result = workflow.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                "LEANFLOW_NATIVE_PROVIDER": "claude",
                "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
                "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
                "LEANFLOW_PROVER_AGENT_CALLS": str(calls),
            }
        )
        directory = tmp_path / ".leanflow/native-workflows" / result["run_id"]
        return result, json.loads((directory / "limits.json").read_text()), requests, source, checks

    return invoke


def test_workflow_promotes_only_after_item_and_coherence_reviews(formalization_workflow):
    result, accounting, requests, source, checks = formalization_workflow()
    assert result["status"] == "draft_ready" and result["source_review"]["accepted"]
    assert source.read_text() == checks[0]["Main.lean"]
    assert len(requests) == 3 and requests[0].tools
    for reviewer in requests[1:]:
        assert not reviewer.tools and reviewer.backend == "claude"
        assert reviewer.session_id == "" and reviewer.previous_usage is None
        assert reviewer.cwd != requests[0].cwd
    assert len(accounting["invocations"]) == 3


def test_workflow_does_not_reroll_unchanged_source_rejection(formalization_workflow):
    result, accounting, requests, source, checks = formalization_workflow(decision="BLOCK", calls=4)
    assert result["status"] == "blocked" and "not rerolled" in result["error"]
    assert source.read_text() == "-- Existing project\n"
    assert len(checks) == 2 and len(requests) == 3
    assert "positivity assumption" in requests[2].prompt
    assert len(accounting["invocations"]) == 3 and "promoted_files" not in result


def test_workflow_review_startup_failure_never_claims_completion(formalization_workflow):
    result, accounting, requests, source, _ = formalization_workflow(calls=2, startup_error=True)
    assert result["status"] == "blocked"
    assert result["source_review"]["status"] == "startup_error"
    assert "authentication unavailable" in result["error"]
    assert source.read_text() == "-- Existing project\n" and len(requests) == 2
    assert [call["status"] for call in accounting["invocations"].values()] == [
        "finished",
        "not_started",
    ]


def test_final_rejected_snapshot_is_checked_once(formalization_workflow):
    result, accounting, requests, source, checks = formalization_workflow(
        calls=1, submit_then_change=True
    )
    assert result["status"] == "limit_reached"
    assert len(checks) == 2  # explicit submission, then exact final snapshot
    assert len(requests) == 1 and source.read_text() == "-- Existing project\n"
    assert "promoted_files" not in result


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        ("check_timeout", "deadline_exceeded"),
        ("check_cancelled", "cancelled"),
        ("isolation_unavailable", "blocked"),
        ("check_setup_failed", "blocked"),
        ("check_artifact_drift", "blocked"),
        ("needs_reprepare", "needs_reprepare"),
    ],
)
def test_checking_error_stops_without_paid_author_repair(
    formalization_workflow, monkeypatch, error_code, status
):
    from leanflow_cli.formalization import proposal

    monkeypatch.setattr(
        proposal,
        "check_proposal",
        lambda **kwargs: {
            "accepted": False,
            "status": "check_error",
            "error_code": error_code,
            "error": "Restore checking infrastructure",
        },
    )
    result, accounting, requests, source, _ = formalization_workflow(calls=5)
    assert result["status"] == status and "infrastructure" in result["error"]
    assert len(requests) == len(accounting["invocations"]) == 1
    assert source.read_text() == "-- Existing project\n"


def test_stale_preparation_submission_interrupts_author_before_native_retry(
    formalization_workflow, monkeypatch
):
    from leanflow_cli.formalization import proposal

    checks = []

    def stale(**kwargs):
        checks.append(kwargs)
        return {
            "accepted": False,
            "status": "check_error",
            "error_code": "needs_reprepare",
            "error": "Prepared dependencies changed; check setup again",
        }

    monkeypatch.setattr(proposal, "check_proposal", stale)
    result, accounting, requests, source, _ = formalization_workflow(
        calls=5, terminal_submission=True
    )
    assert result["status"] == "needs_reprepare"
    assert len(checks) == len(requests) == len(accounting["invocations"]) == 1
    assert source.read_text() == "-- Existing project\n" and "promoted_files" not in result


def test_direct_controller_blocks_unresolved_inventory_before_inference(tmp_path, monkeypatch):
    from leanflow_cli.formalization.records import create_document

    (tmp_path / "paper.tex").write_text(r"\begin{theorem}\Mystery{x} holds.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")

    def forbidden(*args, **kwargs):
        raise AssertionError("Inference must not run for unresolved source extraction")

    monkeypatch.setattr(workflow, "run_harness", forbidden)
    with pytest.raises(ValueError, match="Repair source inventory"):
        workflow.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
                "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
            }
        )


def test_exhausted_review_budget_retains_operational_error_receipt(formalization_workflow):
    from leanflow_cli.formalization.records import load_document, load_receipt

    result, _, _, source, _ = formalization_workflow(calls=1)
    assert result["status"] == "limit_reached"
    directory = next((source.parent / ".leanflow/formalizations").iterdir())
    receipt = load_receipt(load_document(source.parent, directory.name))
    assert receipt["status"] == "review_error" and receipt["review_status"] == "limit_reached"
