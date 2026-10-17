"""Drive real controller/bridge/accounting delivery with explicitly synthetic Lean and model boundaries."""

from __future__ import annotations

import json
import signal
from types import SimpleNamespace

import pytest

from agent.runtime.harness import HarnessResult
from agent.runtime.harness.models import NativeCleanupUnverified
from core.campaign_limits import AccountingError, CampaignLimits
from leanflow_cli.formalization import draft_delivery, preparation, proposal, review, source_draft
from leanflow_cli.formalization.lean_layout import generation_layout
from leanflow_cli.formalization.records import (
    create_document,
    document_directory,
    load_document,
    new_revision,
    save_revision,
)
from leanflow_cli.formalization.service import resolve_artifact, snapshot
from leanflow_cli.native import harness_best_effort as best
from leanflow_cli.native import harness_workflow as runner
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.runtime.workflow_resources import WorkflowResourceFailure


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    """Keep real controller ownership, bridge drainage, ledger writes and preview publication."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LEAN_PATH", raising=False)
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment._toolchain_roots", lambda root: []
    )
    (tmp_path / "Main.lean").write_text("import Init\n")
    (tmp_path / "lakefile.toml").write_text('name = "paper"\n[[lean_lib]]\nname = "Main"\n')
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n")
    source = tmp_path / "paper.tex"
    source.write_text(
        r"\begin{definition}The object is zero.\end{definition}"
        "\n"
        r"\begin{theorem}The object equals zero.\end{theorem}"
    )
    record = create_document(tmp_path, source)
    checked_layout = {
        "accepted": True,
        "src_dir": ".",
        "root_file": "Main.lean",
        "module_prefix": "Main.Formalization.Paper",
        "owned_prefix": "Main/Formalization/Paper",
        "aggregator": "Main/Formalization/Paper.lean",
        "libraries": [{"src_dir": ".", "roots": ["Main"]}],
    }
    record.update(
        workflow_protocol=2,
        preparation_id="fixture-prepared",
        preparation_plan_id="fixture-plan",
        slice_source_ids=[row["id"] for row in record["inventory"]["items"]],
        environment_id="fixture-environment",
        layout=checked_layout,
    )
    base = save_revision(document_directory(tmp_path, record["document_id"]), record)
    allocation = generation_layout(checked_layout, base)
    controlled = {
        "authors": [],
        "reviews": [],
        "advisories": [],
        "retained": [],
        "technical_failure": False,
        "cleanup_boundary": "",
        "cleanup_mode": "",
        "cancel": False,
        "resource_failure": False,
        "resume_checks": [],
    }
    directory = tmp_path / ".leanflow/native-workflows/lifecycle-run"
    environment = {
        "LEANFLOW_PROJECT_ROOT": str(tmp_path),
        "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
        "LEANFLOW_NATIVE_PROVIDER": "codex",
        "LEANFLOW_WORKFLOW_RUN_ID": "lifecycle-run",
        "LEANFLOW_FORMALIZATION_DOCUMENT_ID": base["document_id"],
        "LEANFLOW_FORMALIZATION_REVISION": base["revision_id"],
        "LEANFLOW_FORMALIZATION_CORRECTION_ROUNDS": "0",
        "LEANFLOW_PROVER_AGENT_CALLS": "6",
        "LEANFLOW_PROVER_WALL_TIME_S": "20",
    }
    prepared = {
        "preparation_id": base["preparation_id"],
        "preparation_plan_id": base["preparation_plan_id"],
        "source_bundle": base["source_bundle"],
        "plan": {
            "plan_id": base["preparation_plan_id"],
            "slice_source_ids": base["slice_source_ids"],
            "required_imports": ["Init"],
            "mathematical_prerequisites": [],
            "representation_notes": ["Synthetic lifecycle test only."],
        },
    }
    monkeypatch.setattr(preparation, "load_preparation_receipt", lambda root: prepared)
    monkeypatch.setattr(preparation, "assert_preparation_current", lambda *a, **kw: prepared)
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.preflight_dependencies", lambda *a: checked_layout
    )
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    monkeypatch.setattr(
        "leanflow_cli.formalization.publish.publish_checked",
        lambda *a, **kw: pytest.fail("Unaccepted draft was published"),
    )

    class Resources:
        def __init__(self, environment, cancelled, **kwargs):
            self.cancelled, self.failure = cancelled, None

        def start(self):
            pass

        def check(self, **kwargs):
            if controlled["resource_failure"]:
                self.failure = WorkflowResourceFailure(
                    "Injected aggregate resource failure", code="workflow_memory_limit"
                )
                self.cancelled.set()
                raise self.failure

        def close(self):
            if controlled.get("resource_on_close"):
                self.failure = WorkflowResourceFailure(
                    "Injected final resource failure", code="workflow_memory_limit"
                )
                self.cancelled.set()

        def evidence(self):
            return {"cleanup_verified": True, "synthetic_monitor": True}

    monkeypatch.setattr(runner, "WorkflowResourceWindow", Resources)
    relative = allocation["owned_prefix"] + "/Statements.lean"

    def inspection(*args, **kwargs):
        if controlled["technical_failure"] and len(controlled["authors"]) > 1:
            return {
                "accepted": False,
                "error_code": controlled.get("technical_error", "check_setup_failed"),
                "error": "Injected checker startup failure",
                "declarations": [],
            }
        return {
            "accepted": True,
            "declarations": [
                {"name": "Paper.object", "file": relative},
                {"name": "Paper.result", "file": relative, "kind": "theorem", "pending": True},
            ],
            "local_artifacts": {"fixture-module": {"sha256": "synthetic-artifact"}},
        }

    def frozen_copy(root, target, **kwargs):
        target.mkdir(parents=True)
        for name, data in source_files(root).items():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        return target

    monkeypatch.setattr(best, "environment_identity", lambda root: "fixture-environment")
    monkeypatch.setattr(best, "create_formalization_snapshot", frozen_copy)
    monkeypatch.setattr(best, "inspect_draft", inspection)
    monkeypatch.setattr(
        best,
        "build_project",
        lambda *a, **kw: {"success": True, "local_artifacts": inspection()["local_artifacts"]},
    )

    def checked(**kwargs):
        candidate = new_revision(base)
        candidate.update(
            author_base_revision=base["revision_id"],
            author_run_id="lifecycle-run",
            author_selection_token=kwargs["selection_token"],
            checks=inspection(),
            proposal_phase="representative_slice",
            layout=allocation,
        )
        candidate = save_revision(document_directory(tmp_path, base["document_id"]), candidate)
        return {
            "accepted": True,
            "record": candidate,
            "files": {},
            "revision_id": candidate["revision_id"],
            "selection_token": kwargs["selection_token"],
        }

    monkeypatch.setattr(proposal, "check_proposal", checked)

    def charge(budget):
        invocation = budget.reserve()
        budget.started_call(invocation)
        budget.finish(invocation, {"input_tokens": 3, "output_tokens": 2}, started=True)

    def reviewer(record, **kwargs):
        controlled["reviews"].append(record["revision_id"])
        charge(kwargs["budget"])
        return review_result("source")

    def advisory(*args, **kwargs):
        controlled["advisories"].append(True)
        charge(kwargs["budget"])
        if controlled["cancel"]:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        if controlled.get("resource_after_review"):
            controlled["resource_failure"] = True
        result = review_result("advisory")
        if controlled.get("advisory_status"):
            result.update(status=controlled["advisory_status"], advisory_decision="UNREVIEWED")
        return result

    def review_result(boundary):
        result = {
            "accepted": False,
            "status": "completed",
            "cleanup_verified": True,
            "feedback": "The source notation remains ambiguous; retain the provisional interpretation.",
            "reviews": [],
        }
        if controlled["cleanup_boundary"] == boundary:
            if controlled["cleanup_mode"] == "raised":
                raise NativeCleanupUnverified("Injected independent reviewer closure uncertainty")
            result["cleanup_verified"] = False
            if controlled["cleanup_mode"] == "status":
                result["status"] = "cleanup_unverified"
        return result

    monkeypatch.setattr(review, "review_revision", reviewer)
    monkeypatch.setattr("leanflow_cli.formalization.draft_review.review_draft", advisory)

    def invoke(request, on_event=None):
        controlled["authors"].append(request)
        if controlled.get("assert_reset"):
            current = json.loads((directory / "state.json").read_bytes())
            controlled["resume_checks"].append(current)
            assert "best_effort_check" not in current and "best_effort_review" not in current
            assert current["best_effort_author_completed"] is False
        request.on_started()
        module = request.cwd / relative
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text("def Paper.object : Nat := 0\ntheorem Paper.result : True := by sorry\n")
        (request.cwd / allocation["aggregator"]).write_text(
            "import " + allocation["module_prefix"] + ".Statements\n"
        )
        proposed = {
            "phase": "complete" if len(controlled["authors"]) > 1 else "representative_slice",
            "units": [
                {
                    "id": "all-statements",
                    "required_source_ids": base["slice_source_ids"],
                    "declarations": [
                        {"name": name, "file": relative}
                        for name in ("Paper.object", "Paper.result")
                    ],
                }
            ],
            "coverage": [
                {
                    "region_id": row["id"],
                    "disposition": "unresolved",
                    "item_ids": row.get("item_ids", []),
                    "reason": "Kept explicitly provisional.",
                }
                for row in base["inventory"]["regions"]
            ],
        }
        if controlled.get("missing_mapping") and len(controlled["authors"]) > 1:
            proposed["units"][0]["declarations"].append(
                {"name": "Paper.notDeclared", "file": relative}
            )
        (request.cwd / ".leanflow-job/formalization-proposal.json").write_text(json.dumps(proposed))
        return HarnessResult(
            "completed",
            "Saved all provisional statements and sorry proof slots.",
            "fixture-session",
            usage={"input_tokens": 5, "output_tokens": 4},
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    retain = draft_delivery.retain_best_effort

    def observed_retain(*args, **kwargs):
        controlled["retained"].append(kwargs)
        return retain(*args, **kwargs)

    monkeypatch.setattr(draft_delivery, "retain_best_effort", observed_retain)
    return SimpleNamespace(
        root=tmp_path,
        base=base,
        directory=directory,
        environment=environment,
        control=controlled,
        source_bytes=source.read_bytes(),
        monkeypatch=monkeypatch,
    )


def _ledger(case):
    binding = json.loads((case.directory / "campaign-accounting.json").read_bytes())
    path = case.root / binding["budget_relative"]
    return path, json.loads(path.read_bytes())


def test_review_block_finishes_whole_project_in_same_charged_campaign(lifecycle):
    case = lifecycle
    result = runner.run_workflow(case.environment)
    assert result["status"] == "completed_with_ambiguities", result
    assert len(case.control["authors"]) == 2 and len(case.control["reviews"]) == 1
    assert len(case.control["advisories"]) == len(case.control["retained"]) == 1
    assert "ENTIRE paper" in case.control["authors"][1].prompt
    preview = result["draft_preview"]
    assert preview["delivery_status"] == "complete" and preview["accepted"] is False
    selected = load_document(case.root, case.base["document_id"])
    shown = snapshot(case.root, selected)
    assert shown["handoff"] is None and shown["can_prove"] is False
    project = resolve_artifact(
        case.root,
        selected["document_id"],
        selected["revision_id"],
        preview["delivery"]["project_artifact_id"],
    )
    assert project["kind"] == "project" and (case.directory / "project/Main.lean").is_file()
    _, ledger = _ledger(case)
    assert len(ledger["invocations"]) == 4
    assert all(
        row["status"] == "finished" and row["usage"] for row in ledger["invocations"].values()
    )
    assert (case.root / "paper.tex").read_bytes() == case.source_bytes
    assert (case.root / "Main.lean").read_text() == "import Init\n"
    assert not (case.root / "Main/Formalization").exists()
    assert (
        len(list((case.root / ".leanflow/native-workflows").glob("*/campaign-accounting.json")))
        == 1
    )


def test_final_accounting_fence_failure_prevents_any_preview_selection(lifecycle):
    case = lifecycle

    def failed_finish(self):
        raise AccountingError("Injected final accounting fence failure")

    case.monkeypatch.setattr(CampaignLimits, "finish_checkpoints", failed_finish)
    result = runner.run_workflow(case.environment)
    assert result["status"] == "accounting_error" and result["accounting_unresolved"]
    assert not case.control["retained"] and "draft_preview" not in result
    selected = load_document(case.root, case.base["document_id"])
    assert source_draft.load_preview(case.root, selected) is None
    _, ledger = _ledger(case)
    assert ledger["checkpoint_worker_active"] is True
    assert len(ledger["invocations"]) == 4


@pytest.mark.parametrize("boundary", ["source", "advisory"])
@pytest.mark.parametrize("mode", ["returned", "status", "raised"])
def test_uncertain_independent_reviewer_cannot_be_cleared_or_paid_resumed(
    lifecycle, boundary, mode
):
    case = lifecycle
    case.control.update(cleanup_boundary=boundary, cleanup_mode=mode)
    result = runner.run_workflow(case.environment)
    assert result["independent_cleanup_unverified"] and result["native_cleanup_unverified"]
    assert result["status"] != "completed_with_ambiguities" and not case.control["retained"]
    assert len(case.control["authors"]) == (1 if boundary == "source" else 2)
    path, _ = _ledger(case)
    before = path.read_bytes()
    case.monkeypatch.setattr(
        runner, "run_harness", lambda *a, **kw: pytest.fail("Paid resume after uncertain reviewer")
    )
    case.monkeypatch.setattr(
        "leanflow_cli.formalization.intake.preflight_dependencies",
        lambda *a: pytest.fail("Preflight before fatal-history rejection"),
    )
    with pytest.raises((ValueError, AccountingError), match="closure|unverified|accounting"):
        runner.run_workflow({**case.environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "lifecycle-run"})
    assert path.read_bytes() == before


@pytest.mark.parametrize("stop", ["cancel", "resource_after_review", "resource_on_close"])
def test_explicit_cancel_or_resource_failure_never_completes_or_retains(lifecycle, stop):
    case = lifecycle
    case.control[stop] = True
    result = runner.run_workflow(case.environment)
    assert result["status"] == ("cancelled" if stop == "cancel" else "resource_error"), result
    assert not case.control["retained"] and "draft_preview" not in result


def test_resume_keeps_usage_and_clears_stale_actor_evidence_before_reauthoring(lifecycle):
    case = lifecycle
    case.control["technical_failure"] = True
    first = runner.run_workflow(case.environment)
    assert (
        first["status"] == "draft_incomplete"
        and first["draft_preview"]["delivery_status"] == "incomplete"
    ), first
    path, before = _ledger(case)
    assert len(before["invocations"]) == 3
    old_preview = first["draft_preview"]
    state_path = case.directory / "state.json"
    state = json.loads(state_path.read_bytes())
    state.update(
        best_effort_check={"complete": True, "stale_actor": True},
        best_effort_review={"stale_actor": True},
        best_effort_author_completed=True,
        best_effort_completed=False,
    )
    state_path.write_text(json.dumps(state))
    case.control.update(technical_failure=False, assert_reset=True)
    second = runner.run_workflow(
        {**case.environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "lifecycle-run"}
    )
    assert second["status"] == "completed_with_ambiguities", second
    assert (
        case.control["resume_checks"] and second["draft_preview"]["delivery_status"] == "complete"
    )
    after = json.loads(path.read_bytes())
    assert len(after["invocations"]) == 5
    assert all(after["invocations"][key] == value for key, value in before["invocations"].items())
    assert second["campaign_binding_id"] == first["campaign_binding_id"]
    versions = document_directory(case.root, case.base["document_id"]) / "draft-previews/versions"
    assert (versions / f"{old_preview['preview_id']}.json").is_file()
    assert (versions / f"{second['draft_preview']['preview_id']}.json").is_file()


def test_call_limit_retains_the_last_closed_draft_before_an_unstarted_repair(lifecycle):
    case = lifecycle
    case.environment["LEANFLOW_PROVER_AGENT_CALLS"] = "3"
    case.control["missing_mapping"] = True
    result = runner.run_workflow(case.environment)
    assert result["status"] == "limit_reached", result
    assert len(case.control["authors"]) == 2 and not case.control["advisories"]
    assert len(case.control["retained"]) == 1
    preview = result["draft_preview"]
    assert preview["accepted"] is False and preview["delivery_status"] == "incomplete"
    assert result["best_effort_check"]["missing_compiled_mappings"]
    assert case.control["retained"][0]["review_notes"]["advisory_decision"] == "UNREVIEWED"
    _, ledger = _ledger(case)
    assert len(ledger["invocations"]) == 3
    assert all(row["status"] == "finished" for row in ledger["invocations"].values())
    assert (case.root / "paper.tex").read_bytes() == case.source_bytes


def test_no_advisory_never_attaches_an_earlier_source_review_as_current_notes(lifecycle):
    case = lifecycle
    case.control["technical_failure"] = True
    result = runner.run_workflow(case.environment)
    assert result["status"] == "draft_incomplete"
    assert result["source_review"]["status"] == "completed"
    notes = case.control["retained"][0]["review_notes"]
    assert notes["status"] == "not_run" and notes["advisory_decision"] == "UNREVIEWED"
    assert notes["accepted"] is False and "reviews" not in notes


@pytest.mark.parametrize(
    "review_status", ["limit_reached", "deadline_exceeded", "evidence_unavailable"]
)
def test_unavailable_advisory_remains_visible_without_blocking_complete_draft(
    lifecycle, review_status
):
    case = lifecycle
    case.control["advisory_status"] = review_status
    result = runner.run_workflow(case.environment)
    assert result["status"] == "completed_with_ambiguities", result
    assert result["draft_preview"]["delivery_status"] == "complete"
    assert result["best_effort_review"]["advisory_decision"] == "UNREVIEWED"
    notes = case.control["retained"][0]["review_notes"]
    assert notes["advisory_decision"] == "UNREVIEWED" and notes["status"] == review_status
    assert notes["accepted"] is False
    before_authors = len(case.control["authors"])
    _, before_ledger = _ledger(case)
    resumed = runner.run_workflow(
        {**case.environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "lifecycle-run"}
    )
    assert resumed["draft_preview"]["preview_id"] == result["draft_preview"]["preview_id"]
    assert resumed["best_effort_review"]["advisory_decision"] == "UNREVIEWED"
    assert len(case.control["authors"]) == before_authors
    assert _ledger(case)[1] == before_ledger


@pytest.mark.parametrize(
    ("code", "status"), [("check_cancelled", "cancelled"), ("check_timeout", "deadline_exceeded")]
)
def test_technical_cancellation_or_timeout_keeps_its_operational_status(lifecycle, code, status):
    case = lifecycle
    case.control.update(technical_failure=True, technical_error=code)
    result = runner.run_workflow(case.environment)
    assert result["status"] == status and result["error_code"] == code
    assert not case.control["advisories"]
    assert result["draft_preview"]["delivery_status"] == "incomplete"
    assert result["draft_preview"]["accepted"] is False
