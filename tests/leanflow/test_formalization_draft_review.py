"""Keep best-effort review complete and advisory despite source ambiguities or missing mappings."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import draft_review, review
from leanflow_cli.formalization.clause_obligations import validate_dispositions
from leanflow_cli.formalization.evidence import put_blob
from leanflow_cli.formalization.records import source_bytes
from leanflow_cli.formalization.review_dossier import evidence_documents
from tests.leanflow import test_formalization_clause_obligations as clauses
from tests.leanflow import test_formalization_revision_review as review_helpers


@pytest.fixture
def record(tmp_path: Path) -> dict:
    """Provide frozen source and compiled-shaped fixture rows, without claiming real Lean checks."""
    value = clauses._record(tmp_path, "The generator works for every admissible input.")
    value["checks"]["declarations"][0]["file"] = value["units"][0]["declarations"][0]["file"]
    return value


def test_subject_preserves_complete_sources_and_keeps_corrections_inert(record, tmp_path):
    original = copy.deepcopy(record)
    record["full_inventory"] = copy.deepcopy(record["inventory"])
    record["inventory"]["regions"] = []
    record["workflow_protocol"] = 2
    record["controller_review_scope"] = {"authority": "old-slice"}
    record["correction_authorities"] = [{"id": "user-policy", "enabled": True}]
    proposal = clauses._proposal(record)
    proposal["corrections"] = [
        {
            "source_item_id": record["inventory"]["items"][0]["id"],
            "id": "raw-erratum",
            "approved": True,
        }
    ]
    subject = draft_review.build_advisory_subject(
        record, proposal, record["checks"], directory=tmp_path / "advisory"
    )
    assert subject["inventory"]["regions"] == original["inventory"]["regions"]
    assert source_bytes(subject, "paper.tex") == source_bytes(original, "paper.tex")
    assert subject["revision_id"] == record["revision_id"]
    assert "workflow_protocol" not in subject and "controller_review_scope" not in subject
    assert subject["corrections"] == subject["correction_authorities"] == []
    assert subject["checks"]["accepted"] is False
    assert (
        not subject["accepted"]
        and not subject["publication_authority"]
        and not subject["proof_acceptance"]
    )
    inert = subject["checks"]["draft_advisory"]
    assert inert["authored_proposal"]["corrections"] == proposal["corrections"]
    assert inert["original_correction_authorities"] == record["correction_authorities"]
    assert "raw-erratum" in subject["setup_notes"]
    assert subject["units"][0]["clause_dispositions"][0]["disposition"] == "unresolved"
    validate_dispositions(subject, subject["units"][0], compiled=True)
    documents = evidence_documents(subject)
    assert documents["controller-scope"]["required_source_ids"] == [
        record["inventory"]["items"][0]["id"]
    ]
    assert documents["controller-checks"]["draft_advisory"] == inert
    assert original["checks"] == record["checks"]


def test_missing_mapping_retains_source_and_all_clauses_without_fabricating_lean(record, tmp_path):
    subject = draft_review.build_advisory_subject(
        record, None, record["checks"], directory=tmp_path / "advisory"
    )
    unit = subject["units"][0]
    assert unit["required_source_ids"] == [record["inventory"]["items"][0]["id"]]
    assert unit["declarations"] == []
    assert unit["clause_dispositions"][0]["disposition"] == "unresolved"
    validate_dispositions(subject, unit, compiled=True)


@pytest.mark.parametrize(
    "mutation", ["unknown", "wrong_file", "unhashable", "pending_data", "stale"]
)
def test_invalid_or_stale_authored_references_remain_unresolved(record, tmp_path, mutation):
    proposal = clauses._proposal(record)
    unit = proposal["units"][0]
    if mutation == "unknown":
        unit["declarations"][0]["name"] = "invented"
    elif mutation == "wrong_file":
        unit["declarations"][0]["file"] = "Other.lean"
    elif mutation == "unhashable":
        unit["declarations"][0]["name"] = {}
        unit["clause_dispositions"][0]["disposition"] = []
    elif mutation == "pending_data":
        unit["clause_dispositions"][0]["disposition"] = "pending_proof"
    else:
        proposal["base_revision_id"] = "stale-revision"
    subject = draft_review.build_advisory_subject(
        record, proposal, record["checks"], directory=tmp_path / "advisory"
    )
    assert subject["units"][0]["clause_dispositions"][0]["disposition"] == "unresolved"
    validate_dispositions(subject, subject["units"][0], compiled=True)


def test_independently_checked_refs_replace_author_claims_and_original_files_stay_private(
    record, tmp_path
):
    proposal = clauses._proposal(record)
    proposal["units"][0]["declarations"][0].update(type="False", body_hash="invented")
    subject = draft_review.build_advisory_subject(
        record, proposal, record["checks"], directory=tmp_path / "advisory"
    )
    assert subject["units"][0]["declarations"][0]["type"] == "Nat → Nat"
    assert "body_hash" not in subject["units"][0]["declarations"][0]
    assert Path(subject["_directory"]) != Path(record["_directory"])
    assert (Path(subject["_directory"]) / "blobs").is_dir()
    assert not (Path(subject["_directory"]) / "blobs").is_symlink()


def test_context_files_are_available_from_the_private_evidence_root(record, tmp_path):
    record["context_lean_files"] = {
        "Existing.lean": put_blob(Path(record["_directory"]) / "blobs", b"def existing := 3\n")
    }
    subject = draft_review.build_advisory_subject(
        record, clauses._proposal(record), record["checks"], directory=tmp_path / "advisory"
    )
    assert evidence_documents(subject)["lean:Existing.lean"]["text"] == "def existing := 3\n"


def test_named_prop_sorry_slot_remains_explicit_pending_proof(record, tmp_path):
    proposal = clauses._proposal(record)
    proposal["units"][0]["clause_dispositions"][0]["disposition"] = "pending_proof"
    record["checks"]["declarations"][0].update(
        kind="theorem",
        is_prop=True,
        pending=True,
        data_sorry=False,
        direct_type_sorry=False,
        type_untracked_sorry=False,
    )
    subject = draft_review.build_advisory_subject(
        record, proposal, record["checks"], directory=tmp_path / "advisory"
    )
    assert subject["units"][0]["clause_dispositions"][0]["disposition"] == "pending_proof"
    validate_dispositions(subject, subject["units"][0], compiled=True)
    assert subject["proof_authority"] is False


def test_source_blob_aliases_cannot_enter_private_advisory_evidence(record, tmp_path):
    key = record["source_files"]["paper.tex"]
    original = Path(record["_directory"]) / "blobs" / key
    alias = tmp_path / "aliased-source.tex"
    original.rename(alias)
    original.symlink_to(alias)
    with pytest.raises(ValueError, match="symlink|alias"):
        draft_review.build_advisory_subject(
            record, None, record["checks"], directory=tmp_path / "advisory"
        )


def test_oversized_proposal_is_rejected_without_source_changes(record, tmp_path):
    original = copy.deepcopy(record)
    with pytest.raises(ValueError, match="artifact limit"):
        draft_review.build_advisory_subject(
            record,
            {"notes": "x" * (2 * 1024**2)},
            record["checks"],
            directory=tmp_path / "advisory",
        )
    assert record == original


def test_advisory_review_covers_regions_after_a_block_and_cannot_enter_normal_cache(
    record, tmp_path, monkeypatch
):
    proposal = clauses._proposal(record)
    proposal["units"][0]["clause_dispositions"][0]["disposition"] = "unresolved"
    monkeypatch.setattr(review_helpers, "answer", clauses._answer)
    calls, kwargs = review_helpers._review_transport(record, tmp_path, monkeypatch)
    original = copy.deepcopy(record)
    result = draft_review.review_draft(record, proposal, record["checks"], **kwargs)
    assert result["status"] == "completed" and result["advisory_decision"] == "BLOCK"
    assert len(calls) == 2
    assert [row["phase"] for row in (call["dossier"] for call in calls)] == [
        "translation",
        "coherence",
    ]
    assert {
        row["region_id"] for receipt in result["reviews"] for row in receipt.get("coverage", [])
    } == {row["id"] for row in record["inventory"]["regions"]}
    assert (
        result["accepted"] is False
        and result["publication_authority"] is False
        and result["proof_acceptance"] is False
    )
    assert all(row["advisory"] and row["accepted"] is False for row in result["reviews"])
    assert record == original and not (Path(record["_directory"]) / "review-cache").exists()
    assert list((kwargs["directory"] / "advisory-review").glob("*/subject/review-cache/*.json"))
    draft_review.review_draft(record, proposal, record["checks"], **kwargs)
    assert (
        len(calls) == 4
    ), "Every advisory invocation uses fresh review sessions, not normal cache reuse"


def test_normal_review_still_stops_on_the_original_semantic_gate(record, tmp_path, monkeypatch):
    record["units"][0]["clause_dispositions"][0]["disposition"] = "unresolved"
    monkeypatch.setattr(review_helpers, "answer", clauses._answer)
    calls, kwargs = review_helpers._review_transport(record, tmp_path, monkeypatch)
    result = review.review_revision(record, **kwargs)
    assert not result["accepted"] and len(calls) == 1


def test_even_a_positive_advisory_review_has_no_acceptance_authority(record, tmp_path, monkeypatch):
    monkeypatch.setattr(review_helpers, "answer", clauses._answer)
    _, kwargs = review_helpers._review_transport(record, tmp_path, monkeypatch)
    result = draft_review.review_draft(
        record, clauses._proposal(record), record["checks"], **kwargs
    )
    assert result["advisory_decision"] == "PASS" and result["accepted"] is False
    assert all(
        row["advisory_decision"] == "PASS" and row["accepted"] is False for row in result["reviews"]
    )
    assert all(
        verdict["advisory"] and verdict["accepted"] is False
        for row in result["reviews"]
        for verdict in row["verdicts"]
    )


def test_no_remaining_calls_retains_an_explicit_unreviewed_result(record, tmp_path, monkeypatch):
    def forbidden(request):
        pytest.fail("A zero-call advisory budget cannot launch a reviewer")

    monkeypatch.setattr(review, "run_harness", forbidden)
    result = draft_review.review_draft(
        record,
        clauses._proposal(record),
        record["checks"],
        backend="codex",
        model="model",
        effort="max",
        environment={"LEANFLOW_PROJECT_ROOT": str(tmp_path)},
        directory=tmp_path / "review",
        budget=CampaignLimits(tmp_path / "budget.json", {"agent_calls": 0}),
    )
    assert result["status"] == "limit_reached" and result["advisory_decision"] == "UNREVIEWED"
    assert result["accepted"] is False and not result["reviews"]
