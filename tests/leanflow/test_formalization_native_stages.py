"""Drive source correction, reviewed representative slices and full expansion through one native budget."""

from __future__ import annotations

import json

from agent.runtime.harness import HarnessResult, IsolationPolicy
from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import preparation, proposal, publish, review, slice_gate
from leanflow_cli.formalization.evidence import digest
from leanflow_cli.formalization.proposal_schema import apply_proposal
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_directory,
    save_revision,
)
from leanflow_cli.formalization.review_scope import bind_review_scope
from leanflow_cli.native import harness_workflow as native
from leanflow_cli.workflows.prover.lean_environment import lean_environment_generation


def test_native_corrects_slice_then_expands_all_reuse_without_proof_or_premature_publication(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment._toolchain_roots", lambda root: []
    )
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.preflight_dependencies", lambda *args: {}
    )
    monkeypatch.setattr(
        "leanflow_cli.native.harness_context.load_skill", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        preparation,
        "assert_preparation_current",
        lambda *args, **kwargs: {"preparation_id": "prepared", "environment_id": "environment"},
    )
    (tmp_path / "Main.lean").write_text("import Init\n")
    (tmp_path / "paper.tex").write_text(
        r"\begin{definition}Define true.\end{definition}"
        "\n"
        r"\begin{theorem}True holds.\end{theorem}"
        "\n"
    )
    record = create_document(tmp_path, tmp_path / "paper.tex")
    record.update(
        workflow_protocol=2,
        preparation_id="prepared",
        preparation_plan_id="plan",
        slice_source_ids=[row["id"] for row in record["inventory"]["items"]],
        environment_id="environment",
        layout={
            "root_file": "Main.lean",
            "module_prefix": "Main.Formalization.Paper",
            "aggregator": "Main/Formalization/Paper.lean",
        },
    )
    base = save_revision(document_directory(tmp_path, record["document_id"]), record)
    budget_path = tmp_path / ".leanflow/formalization-campaigns/fixture/budget.json"
    budget = CampaignLimits(budget_path, {"agent_calls": 9, "tokens": None, "wall_time_s": None})
    setup_call = budget.reserve()
    budget.started_call(setup_call)
    budget.finish(setup_call, {"input_tokens": 1, "output_tokens": 1}, started=True)
    monkeypatch.setattr(
        preparation,
        "load_preparation_receipt",
        lambda root: {
            "preparation_id": "prepared",
            "preparation_plan_id": "plan",
            "budget_path": str(budget_path),
            "source_bundle": base["source_bundle"],
            "plan": {
                "plan_id": "plan",
                "slice_source_ids": base["slice_source_ids"],
                "required_imports": ["Init"],
                "mathematical_prerequisites": [],
                "representation_notes": ["Exact True meaning requires source review"],
            },
        },
    )
    for module in (native, review):
        monkeypatch.setattr(
            module, "build_runtime_isolation", lambda backend, cwd, **kwargs: IsolationPolicy(cwd)
        )
    authors, reviewers, publications = [], [], []

    def checked(**kwargs):
        submitted = json.loads(
            (kwargs["snapshot"] / ".leanflow-job/formalization-proposal.json").read_text()
        )
        slice_gate.require_stage(tmp_path, base, submitted["phase"])
        candidate = apply_proposal(base, submitted)
        candidate.update(
            lean_files={},
            context_lean_files={},
            project_source_hashes={name: digest(data) for name, data in kwargs["baseline"].items()},
        )
        candidate["checks"] = {
            "accepted": True,
            "declarations": [
                {
                    "name": "True" if index == 0 else "True.intro",
                    "module": "Init.Prelude",
                    "kind": "definition" if index == 0 else "theorem",
                    "type_hash": "wrong" if len(authors) == 1 else "right",
                    "body_hash": "body",
                    "declaration_origin": "dependency",
                    "conditional": False,
                    "depends_on": [],
                }
                for index in range(2)
            ],
            "obligations": [],
            "modules": {},
        }
        bind_review_scope(tmp_path, candidate)
        candidate = save_revision(document_directory(tmp_path, base["document_id"]), candidate)
        if candidate["proposal_phase"] == "complete":
            slice_gate.assert_complete_slice(tmp_path, candidate)
        return {
            "accepted": True,
            "record": candidate,
            "files": {},
            "revision_id": candidate["revision_id"],
            "selection_token": current_selection_token(tmp_path, base["document_id"]),
            "environment_generation": lean_environment_generation(tmp_path),
        }

    monkeypatch.setattr(proposal, "check_proposal", checked)

    def publisher(root, baseline, metadata, result, reviewed, directory, **kwargs):
        assert result["record"]["proposal_phase"] == "complete" and reviewed["accepted"]
        slice_gate.assert_complete_slice(root, result["record"])
        publications.append(result)
        return {"status": "published"}

    monkeypatch.setattr(publish, "publish_checked", publisher)

    def invoke(request, on_event=None):
        request.on_started()
        if request.tools:
            authors.append(request)
            phase = slice_gate.slice_status(tmp_path, base)["phase"]
            target = request.cwd / ".leanflow-job/formalization-proposal.json"
            target.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "base_revision_id": base["revision_id"],
                        "phase": phase,
                        "units": [
                            {
                                "id": "unit-" + str(index),
                                "required_source_ids": [item["id"]],
                                "declarations": [
                                    {
                                        "name": "True" if index == 0 else "True.intro",
                                        "imports": ["Init"],
                                    }
                                ],
                                "representation_note": (
                                    "wrong meaning" if len(authors) == 1 else "Exact True meaning"
                                ),
                                "depends_on_units": [],
                            }
                            for index, item in enumerate(base["inventory"]["items"])
                        ],
                        "coverage": [
                            {
                                "region_id": row["id"],
                                "disposition": row["disposition"],
                                "item_ids": row.get("item_ids", []),
                                "reason": row["reason"] or "Exact source region",
                            }
                            for row in base["inventory"]["regions"]
                            if phase == "complete" or row["id"] in slice_gate.slice_regions(base)
                        ],
                    }
                )
            )
            return HarnessResult(
                "completed",
                "Draft submitted",
                "author-session",
                {"input_tokens": 1, "output_tokens": 1},
            )
        reviewers.append(request)
        assert not request.session_id and request.restricted_tools
        assignment = json.loads(request.prompt[len(review._CONTRACT) + 1 :])
        dossier = assignment["dossier"]
        decision = "BLOCK" if len(authors) == 1 else "PASS"
        return HarnessResult(
            "completed",
            json.dumps(
                {
                    "revision_id": assignment["revision_id"],
                    "dossier_id": assignment["dossier_id"],
                    "verdicts": [
                        {
                            "unit_id": unit["id"],
                            "source_ids": unit["required_source_ids"],
                            "decision": decision,
                            "rationale": "Exact source meaning inspected",
                            "issues": ["Correct the meaning"] if decision == "BLOCK" else [],
                        }
                        for unit in dossier["units"]
                    ],
                    "coverage": [
                        {
                            "region_id": region["id"],
                            "decision": decision,
                            "rationale": "Exact region inspected",
                        }
                        for region in dossier["regions"]
                    ],
                    "claims": [
                        {
                            "source_id": key,
                            "status": "defined" if index == 0 else "asserted",
                            "rationale": "Source establishes this role",
                        }
                        for index, key in enumerate(
                            sorted(
                                {
                                    key
                                    for unit in dossier["units"]
                                    for key in unit["required_source_ids"]
                                }
                            )
                        )
                    ],
                    "coherence_decision": decision,
                    "feedback": (
                        "Correct the meaning" if decision == "BLOCK" else "Exact source verified"
                    ),
                }
            ),
            "fresh-reviewer",
            {"input_tokens": 1, "output_tokens": 1},
        )

    monkeypatch.setattr(native, "run_harness", invoke)
    monkeypatch.setattr(review, "run_harness", invoke)
    result = native.run_workflow(
        {
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            "LEANFLOW_NATIVE_PROVIDER": "claude",
            "LEANFLOW_FORMALIZATION_DOCUMENT_ID": base["document_id"],
            "LEANFLOW_FORMALIZATION_REVISION": base["revision_id"],
            "LEANFLOW_FORMALIZATION_BUDGET_PATH": str(budget_path),
            "LEANFLOW_PROVER_AGENT_CALLS": "9",
        }
    )
    assert result["status"] == "draft_ready", result
    assert len(authors) == 3 and len(reviewers) == 5 and len(publications) == 1
    assert (
        "representative_slice" in authors[0].prompt and "Correct the meaning" in authors[1].prompt
    )
    assert "slice review passed" in authors[2].prompt
    assert result["formalization_phase"] == "complete" and result["representative_slice"]
    assert (
        publications[0]["record"]["checks"]["obligations"] == [] and result["promoted_files"] == []
    )
    assert (tmp_path / "Main.lean").read_text() == "import Init\n"
    accounting = json.loads(budget_path.read_text())
    assert len(accounting["invocations"]) == 9
