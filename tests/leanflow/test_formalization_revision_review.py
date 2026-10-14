"""Exercise exact source verdicts, cache invalidation, and rejected revision selection."""

import copy
import json
from pathlib import Path

import pytest

from agent.runtime.harness import HarnessResult
from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import proposal, review
from leanflow_cli.formalization.evidence import digest, put_blob
from leanflow_cli.formalization.proposal_schema import apply_proposal, bind_declarations
from leanflow_cli.formalization.records import create_document, load_document, load_receipt
from leanflow_cli.formalization.review_dossier import (
    EvidenceBridge,
    evidence_documents,
    mandatory_dossier,
    review_assignment,
)
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.workflows.prover.native_snapshot import create_snapshot
from leanflow_cli.workflows.prover.session_tools import SessionTools


@pytest.fixture
def source_record(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}\label{thm:t}True.\end{theorem}" + "\n")
    record = create_document(tmp_path, source)
    item = record["inventory"]["items"][0]
    unit = {
        "id": "unit-t",
        "required_source_ids": [item["id"]],
        "declarations": [{"file": "Main/Formalization/Doc/Statements.lean", "name": "t"}],
        "representation_note": "The source assertion is True.",
        "depends_on_units": [],
    }
    raw = b"theorem t : True := by sorry\n"
    record.update(
        units=[unit],
        layout={"library": "Main"},
        environment_id="env-v1",
        lean_files={
            unit["declarations"][0]["file"]: put_blob(Path(record["_directory"]) / "blobs", raw)
        },
        checks={"accepted": True, "declarations": [{"name": "t", "type": "True"}]},
    )
    return record


def answer(dossier, revision, key, *, decision="PASS"):
    return json.dumps(
        {
            "revision_id": revision,
            "dossier_id": key,
            "verdicts": [
                {
                    "unit_id": unit["id"],
                    "source_ids": unit["required_source_ids"],
                    "decision": decision,
                    "rationale": "Checked the exact source and type.",
                    "issues": [] if decision == "PASS" else ["Repair translation."],
                }
                for unit in dossier["units"]
            ],
            "coverage": [
                {
                    "region_id": region["id"],
                    "decision": decision,
                    "rationale": "Audited original region.",
                }
                for region in dossier["regions"]
            ],
            "claims": [
                {
                    "source_id": source_id,
                    "status": "asserted",
                    "rationale": "Verified source content asserts this claim.",
                }
                for source_id in sorted(
                    {key for unit in dossier["units"] for key in unit["required_source_ids"]}
                )
            ],
            "coherence_decision": decision,
            "feedback": "All required evidence examined.",
        }
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_revision",
        "missing_unit",
        "duplicate_unit",
        "missing_source",
        "invented_source",
        "missing_region",
        "pass_with_issue",
    ],
)
def test_review_rejects_incomplete_or_misbound_verdicts(source_record, mutation):
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    value = json.loads(answer(dossier, "rev-a", "key"))
    if mutation == "wrong_revision":
        value["revision_id"] = "rev-b"
    elif mutation == "missing_unit":
        value["verdicts"] = []
    elif mutation == "duplicate_unit":
        value["verdicts"] *= 2
    elif mutation == "missing_source":
        value["verdicts"][0]["source_ids"] = []
    elif mutation == "invented_source":
        value["verdicts"][0]["source_ids"] = ["fake"]
    elif mutation == "missing_region":
        value["coverage"] = []
    else:
        value["verdicts"][0]["issues"] = ["This is still wrong."]
    with pytest.raises(ValueError):
        review.validate_review(json.dumps(value), dossier, "rev-a", "key")


@pytest.mark.parametrize("decision", ["PASS", "BLOCK"])
def test_exact_positive_and_negative_reviews_are_reused_without_reroll(
    source_record, tmp_path, monkeypatch, decision
):
    calls = []

    def invoke(request):
        calls.append(request)
        request.on_started()
        assert request.session_id == "" and not request.tools and request.restricted_tools
        assignment = json.loads(request.prompt[len(review._CONTRACT) + 1 :])
        return HarnessResult(
            status="completed",
            session_id=f"fresh-{len(calls)}",
            final_response=answer(
                assignment["dossier"],
                assignment["revision_id"],
                assignment["dossier_id"],
                decision=decision,
            ),
        )

    monkeypatch.setattr(review, "run_harness", invoke)
    monkeypatch.setattr(review, "build_runtime_isolation", lambda *a, **kw: object())
    budget = CampaignLimits(tmp_path / "limits.json", {})
    kwargs = dict(
        backend="claude",
        model="requested-model",
        effort="max",
        environment={"LEANFLOW_PROJECT_ROOT": str(tmp_path)},
        directory=tmp_path / "review",
        budget=budget,
    )
    first = review.review_revision(source_record, **kwargs)
    count = len(calls)
    assert count == (2 if decision == "PASS" else 1)
    second = review.review_revision(source_record, **kwargs)
    assert len(calls) == count and first["accepted"] == second["accepted"]
    assert all(row["cache_reused"] for row in second["reviews"])
    changed = copy.deepcopy(source_record)
    changed["environment_id"] = "changed-transitive-import"
    review.review_revision(changed, **kwargs)
    assert len(calls) == 2 * count


def test_frozen_reviewer_reads_complete_paginated_text_and_cannot_execute(source_record, tmp_path):
    workspace = tmp_path / "review"
    workspace.mkdir()
    tools = SessionTools(
        role="reviewer",
        project_root=workspace,
        workspace=workspace / "tools",
        context={},
        allow_internet=False,
    )
    bridge = EvidenceBridge(toolset=tools, context={}, callbacks={}, directory=workspace / "bridge")
    text = "x" * 160000 + "required trailing qualifier"
    bridge.documents = {"long-source": {"text": text}}
    with bridge:
        chunks = []
        offset = 0
        while offset is not None:
            result = bridge.call("read_evidence", {"id": "long-source", "offset": offset})
            chunks.append(result["text"])
            offset = result["next_offset"]
        assert json.loads("".join(chunks))["text"] == text
        for name in ("start_command", "submit_files", "start_lean_check"):
            with pytest.raises(ValueError):
                bridge.call(name, {})
        with pytest.raises(ValueError):
            bridge.call("read_evidence", {"id": "../../secret", "offset": 0})


def author_proposal(record):
    return {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "units": record["units"],
        "coverage": [
            {
                "region_id": row["id"],
                "disposition": "item" if row.get("item_ids") else "non_mathematical",
                "item_ids": row.get("item_ids", []),
                "reason": "Audited complete original region",
            }
            for row in record["inventory"]["regions"]
        ],
    }


def test_coverage_denominator_includes_regions_without_detected_items(source_record):
    value = author_proposal(source_record)
    source_record["inventory"]["regions"].append(
        {"id": "prose", "disposition": "unresolved", "item_ids": []}
    )
    with pytest.raises(ValueError, match="every selected document region"):
        apply_proposal(source_record, value)


def test_rejected_submission_replaces_prior_selection(source_record, tmp_path, monkeypatch):
    from leanflow_cli.formalization.records import current_selection_token

    baseline = source_files(tmp_path)
    snapshot = create_snapshot(tmp_path, tmp_path / ".leanflow" / "author")
    job = snapshot / ".leanflow-job"
    job.mkdir()
    (job / "formalization-proposal.json").write_text("{malformed")
    before = load_document(tmp_path, source_record["document_id"])["revision_id"]
    result = proposal.check_proposal(
        root=tmp_path,
        snapshot=snapshot,
        baseline=baseline,
        directory=tmp_path / ".leanflow" / "checks",
        document_id=source_record["document_id"],
        base_revision_id=before,
        selection_token=current_selection_token(tmp_path, source_record["document_id"]),
        allowed_axioms=(),
    )
    selected = load_document(tmp_path, source_record["document_id"])
    assert not result["accepted"] and selected["revision_id"] != before
    assert selected["status"] == "submission_pending" and not selected["checks"]
    receipt = load_receipt(selected)
    assert receipt["status"] == "check_rejected" and receipt["checks"]["error"]


def test_frozen_evidence_does_not_follow_live_source_changes(source_record, tmp_path):
    original = evidence_documents(source_record)["source:paper.tex"]["text"]
    (tmp_path / "paper.tex").write_text("externally changed statement")
    assert evidence_documents(source_record)["source:paper.tex"]["text"] == original
    assert digest(original) == source_record["source_files"]["paper.tex"]


def test_large_review_assignment_pages_complete_evidence_without_truncation(source_record):
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    trailing = "required trailing quantifier"
    dossier["source_items"][0]["text"] = "x" * 5_000_000 + trailing
    assignment, documents = review_assignment(source_record, dossier, "large-key")
    assert len(json.dumps(assignment)) < 2000
    assert assignment["dossier_ref"] == "assignment-dossier"
    assert documents["assignment-dossier"]["source_items"][0]["text"].endswith(trailing)
    assert assignment["unit_count"] == len(dossier["units"])
    assert assignment["region_count"] == len(dossier["regions"])


@pytest.mark.parametrize("status", ["conjectured", "open_question", "unknown"])
def test_open_claim_proof_assignment_requires_explicit_user_intent(source_record, status):
    source_record["checks"]["declarations"][0]["pending"] = True
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    value = json.loads(answer(dossier, "rev-a", "key"))
    value["claims"][0]["status"] = status
    assert not review.validate_review(json.dumps(value), dossier, "rev-a", "key")["accepted"]
    dossier["source_items"][0]["proof_authorization"] = {
        "actor": "user",
        "authorized": True,
        "reason": "I explicitly want to try this open claim.",
    }
    assert review.validate_review(json.dumps(value), dossier, "rev-a", "key")["accepted"]


def test_proved_conjecture_title_uses_reviewed_content_status(source_record):
    source_record["checks"]["declarations"][0]["pending"] = True
    source_record["inventory"]["items"][0]["claim_status"] = "conjectured"
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    value = json.loads(answer(dossier, "rev-a", "key"))
    value["claims"][0].update(
        status="proved", rationale="The source supplies a complete proof despite its title."
    )
    assert review.validate_review(json.dumps(value), dossier, "rev-a", "key")["accepted"]


def test_unmapped_helper_proof_cannot_avoid_source_authorization(source_record):
    source_record["units"][0]["required_source_ids"] = []
    row = source_record["checks"]["declarations"][0]
    row.update(pending=True, type_hash="type", body_hash="body", direct_dependencies=[])
    row["file"] = source_record["units"][0]["declarations"][0]["file"]
    with pytest.raises(ValueError, match="requires source provenance"):
        bind_declarations(source_record, source_record["checks"])


def test_helper_type_dependency_inherits_open_question_authorization(source_record):
    unit = source_record["units"][0]
    source_id = unit["required_source_ids"][0]
    unit["declarations"][0]["name"] = "Question"
    source_record["units"].append(
        {
            "id": "helper",
            "required_source_ids": [],
            "declarations": [{"file": unit["declarations"][0]["file"], "name": "prove_question"}],
            "representation_note": "Helper claiming the source question",
            "depends_on_units": [],
        }
    )
    source_record["checks"]["declarations"] = [
        {
            "name": "Question",
            "type": "Prop",
            "type_hash": "type-q",
            "body_hash": "body-q",
            "pending": False,
        },
        {
            "name": "prove_question",
            "type": "Question",
            "type_hash": "type-proof",
            "body_hash": "sorry",
            "pending": True,
            "direct_dependencies": ["Question"],
        },
    ]
    for row in source_record["checks"]["declarations"]:
        row["file"] = unit["declarations"][0]["file"]
    bind_declarations(source_record, source_record["checks"])
    helper = source_record["units"][1]
    assert helper["required_source_ids"] == [source_id] and helper["derived_source_ids"] == [
        source_id
    ]
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    response = json.loads(answer(dossier, "rev-a", "key"))
    response["claims"][0]["status"] = "open_question"
    assert not review.validate_review(json.dumps(response), dossier, "rev-a", "key")["accepted"]


def test_translation_dependencies_are_ordered_and_cycles_block(source_record):
    value = author_proposal(source_record)
    value["units"] = copy.deepcopy(value["units"])
    value["units"][0]["depends_on_units"] = ["helper"]
    helper = {
        **copy.deepcopy(value["units"][0]),
        "id": "helper",
        "required_source_ids": [],
        "depends_on_units": [],
    }
    value["units"].append(helper)
    assert [u["id"] for u in apply_proposal(source_record, value)["units"]] == ["helper", "unit-t"]
    helper["depends_on_units"] = ["unit-t"]
    with pytest.raises(ValueError, match="dependency cycle"):
        apply_proposal(source_record, value)


def test_region_evidence_uses_original_byte_offsets_with_unicode_and_json_escaping(
    source_record, tmp_path
):
    raw = 'Préface "quoted".\n\\begin{theorem}For α, True.\\end{theorem}\n'.encode()
    source_record["source_files"]["paper.tex"] = put_blob(
        Path(source_record["_directory"]) / "blobs", raw
    )
    regions = source_record["inventory"]["regions"]
    start = raw.index(b"For")
    regions[0].update(byte_start=start, byte_end=start + len("For α, True.".encode()))
    document = evidence_documents(source_record)["region:" + regions[0]["id"]]
    assert document["original_text"] == "For α, True."
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    assert dossier["regions"][0]["original_text"] == "For α, True."


def test_unchanged_project_definition_is_available_as_frozen_review_evidence(source_record):
    content = b"def ExistingMeaning : Prop := False\n"
    source_record["context_lean_files"] = {
        "Existing.lean": put_blob(Path(source_record["_directory"]) / "blobs", content)
    }
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=False)
    assert "lean:Existing.lean" in dossier["evidence_catalog"]
    _, documents = review_assignment(source_record, dossier, "key")
    assert documents["lean:Existing.lean"]["text"] == content.decode()


def _review_transport(source_record, tmp_path, monkeypatch, *, decision="PASS", malformed=False):
    calls = []
    bridges = []
    bridge_type = review.EvidenceBridge

    def evidence_bridge(**kwargs):
        bridge = bridge_type(**kwargs)
        bridges.append(bridge)
        return bridge

    def invoke(request):
        assignment = json.loads(request.prompt[len(review._CONTRACT) + 1 :])
        if "dossier_ref" in assignment:
            offset, chunks = 0, []
            while offset is not None:
                page = bridges[-1].call(
                    "read_evidence", {"id": assignment["dossier_ref"], "offset": offset}
                )
                chunks.append(page["text"])
                offset = page["next_offset"]
            assignment = {**assignment, "dossier": json.loads("".join(chunks))}
        calls.append(assignment)
        request.on_started()
        result = answer(
            assignment["dossier"],
            assignment["revision_id"],
            assignment["dossier_id"],
            decision=decision,
        )
        if malformed and len(calls) == 1:
            result = "not a review response"
        return HarnessResult(status="completed", final_response=result)

    monkeypatch.setattr(review, "run_harness", invoke)
    monkeypatch.setattr(review, "EvidenceBridge", evidence_bridge)
    monkeypatch.setattr(review, "build_runtime_isolation", lambda *a, **kw: object())
    return calls, dict(
        backend="claude",
        model="fixture",
        effort="max",
        environment={"LEANFLOW_PROJECT_ROOT": str(tmp_path)},
        directory=tmp_path / "review",
        budget=CampaignLimits(tmp_path / "limits.json", {}),
    )


def test_fenced_json_is_valid_and_invalid_protocol_is_not_negative_cached(
    source_record, tmp_path, monkeypatch
):
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    assert review.validate_review(
        "```json\n" + answer(dossier, "rev", "key") + "\n```", dossier, "rev", "key"
    )["accepted"]
    calls, kwargs = _review_transport(source_record, tmp_path, monkeypatch, malformed=True)
    first = review.review_revision(source_record, **kwargs)
    assert first["status"] == "invalid_review" and not first["accepted"]
    assert not list((Path(source_record["_directory"]) / "review-cache").glob("*.json"))
    assert review.review_revision(source_record, **kwargs)["accepted"]
    assert len(calls) == 3


def test_large_review_bounds_response_size_and_covers_every_unit_and_region(
    source_record, tmp_path, monkeypatch
):
    source_record["units"] = [{**source_record["units"][0], "id": f"u-{i}"} for i in range(17)]
    source_record["inventory"]["regions"] = [
        {**source_record["inventory"]["regions"][0], "id": f"r-{i}"} for i in range(81)
    ]
    calls, kwargs = _review_transport(source_record, tmp_path, monkeypatch)
    result = review.review_revision(source_record, **kwargs)
    assert result["accepted"] and len(calls) == 7
    assert all(
        len(call["dossier"]["units"]) <= 8 and len(call["dossier"]["regions"]) <= 40
        for call in calls
    )
    assert {row["region_id"] for result in result["reviews"] for row in result["coverage"]} == {
        f"r-{i}" for i in range(81)
    }
    final = calls[-1]["dossier"]
    assert final["phase"] == "coherence" and len(final["prior_reviews"]) == 6
    assert len(final["project_unit_ids"]) == 17 and len(final["project_region_ids"]) == 81


def test_rejections_collect_all_batch_feedback_before_correction(
    source_record, tmp_path, monkeypatch
):
    source_record["units"] = [{**source_record["units"][0], "id": f"u-{i}"} for i in range(17)]
    calls, kwargs = _review_transport(source_record, tmp_path, monkeypatch, decision="BLOCK")
    result = review.review_revision(source_record, **kwargs)
    assert not result["accepted"] and result["status"] == "completed"
    assert len([c for c in calls if c["dossier"]["phase"] == "translation"]) == 3
    assert all(c["dossier"]["phase"] != "coherence" for c in calls)
    assert {v["unit_id"] for r in result["reviews"] for v in r["verdicts"]} == {
        f"u-{i}" for i in range(17)
    }


def test_lean_comment_changes_cannot_reroll_semantic_rejection(
    source_record, tmp_path, monkeypatch
):
    calls, kwargs = _review_transport(source_record, tmp_path, monkeypatch, decision="BLOCK")
    first = review.review_revision(source_record, **kwargs)
    assert not first["accepted"] and len(calls) == 1
    file = next(iter(source_record["lean_files"]))
    source_record["lean_files"][file] = put_blob(
        Path(source_record["_directory"]) / "blobs",
        b"-- unrelated comment\ntheorem t : True := by sorry -- changed note\n",
    )
    result = review.review_revision(source_record, **kwargs)
    assert not result["accepted"] and result["reviews"][0]["cache_reused"] and len(calls) == 1
    source_record["lean_files"][file] = put_blob(
        Path(source_record["_directory"]) / "blobs", b"theorem t : False := by sorry\n"
    )
    review.review_revision(source_record, **kwargs)
    assert len(calls) == 2


def test_negative_cache_preserves_literal_contents(source_record):
    from leanflow_cli.formalization.review_cache import rejection_key

    dossier = mandatory_dossier(source_record, source_record["units"], coherence=False)
    file = next(iter(source_record["lean_files"]))
    keys = []
    for literal in ('"-- true"', '"-- false"'):
        source_record["lean_files"][file] = put_blob(
            Path(source_record["_directory"]) / "blobs", f"def message := {literal}\n".encode()
        )
        keys.append(rejection_key(source_record, dossier, {}))
    assert keys[0] != keys[1]
