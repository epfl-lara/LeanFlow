"""Keep reviewer scope controller-owned while retaining the complete source denominator."""

import copy
import json

import pytest

from leanflow_cli.formalization import preparation, review
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.proposal_schema import apply_proposal
from leanflow_cli.formalization.review_cache import rejection_key
from leanflow_cli.formalization.review_dossier import (
    evidence_documents,
    mandatory_dossier,
    review_assignment,
)
from leanflow_cli.formalization.review_scope import bind_review_scope, review_scope
from tests.leanflow.test_formalization_slice_gate import _checked, _proposal
from tests.leanflow.test_formalization_slice_gate import prepared as _prepared

prepared = _prepared


def test_slice_coherence_dossier_exposes_controller_phase_instead_of_author_scope_notes(prepared):
    root, base = prepared
    checked = _checked(root, base, phase="representative_slice")
    record = checked["record"]
    bind_review_scope(root, record)
    record["setup_notes"] = "The author claims permission to omit the rest."
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    assert dossier["controller_scope"]["proposal_phase"] == "representative_slice"
    assert dossier["controller_scope"]["preparation_id"] == base["preparation_id"]
    assert dossier["controller_scope"]["preparation_plan_id"] == base["preparation_plan_id"]
    assert dossier["controller_scope"]["full_source_count"] == len(base["inventory"]["items"])
    assert dossier["controller_scope"]["remaining_planned_source_count"] == 1
    assert dossier["controller_scope"]["publication_authority"] is False


def test_slice_scope_keeps_exact_canonical_denominator_and_all_context_readable(prepared):
    root, base = prepared
    before = copy.deepcopy(base["inventory"])
    record = _checked(root, base, phase="representative_slice")["record"]
    bind_review_scope(root, record)
    docs = evidence_documents(record)
    scope = docs["controller-scope"]
    assert docs["full-inventory"] == before == complete_inventory(record)
    assert scope["required_source_ids"] == sorted(base["slice_source_ids"])
    assert set(scope["required_source_ids"]) | set(scope["remaining_planned_source_ids"]) == {
        item["id"] for item in before["items"]
    }
    assert set(scope["required_region_ids"]) | set(scope["remaining_planned_region_ids"]) == {
        region["id"] for region in before["regions"]
    }
    assert scope["deferral"] == "requires-complete-expansion"
    assert not scope["permanent_exclusion_authority"]
    assert not scope["publication_authority"] and not scope["proof_acceptance"]
    for region_id in scope["remaining_planned_region_ids"]:
        assert "original_text" in docs["region:" + region_id]
    assert "Addition is commutative" in docs["source:paper.tex"]["text"]


def test_complete_scope_has_no_deferred_members_and_author_scope_cannot_override_it(prepared):
    root, base = prepared
    proposal = _proposal(base, phase="complete")
    proposal["controller_review_scope"] = {
        "proposal_phase": "representative_slice",
        "proof_acceptance": True,
    }
    record = apply_proposal(base, proposal)
    record["checks"] = {"accepted": True}
    record["setup_notes"] = "The author excludes every source except the first definition."
    bind_review_scope(root, record)
    scope = review_scope(record)
    assert scope["proposal_phase"] == "complete"
    assert scope["remaining_planned_source_ids"] == scope["remaining_planned_region_ids"] == []
    assert scope["required_source_ids"] == scope["included_source_ids"]
    assert scope["required_region_ids"] == scope["full_region_ids"]
    assert scope["proof_acceptance"] is False


@pytest.mark.parametrize(
    "field",
    [
        "document_id",
        "source_bundle",
        "environment_id",
        "preparation_id",
        "preparation_plan_id",
        "proposal_phase",
        "slice_source_ids",
        "source_files",
        "full_inventory",
    ],
)
def test_changed_protected_binding_cannot_reuse_frozen_scope(prepared, field):
    root, base = prepared
    record = _checked(root, base, phase="representative_slice")["record"]
    bind_review_scope(root, record)
    if field == "proposal_phase":
        record[field] = "complete"
    elif field == "slice_source_ids":
        record[field] = [record["slice_source_ids"][0]]
    elif field == "source_files":
        record[field]["paper.tex"] = "changed-source"
    elif field == "full_inventory":
        record[field]["items"].pop()
    else:
        record[field] = "changed-identity"
    with pytest.raises(ValueError, match="review scope"):
        review_scope(record)


@pytest.mark.parametrize(
    "field",
    [
        "required_source_ids",
        "required_region_ids",
        "remaining_planned_source_ids",
        "remaining_planned_region_ids",
        "publication_authority",
        "proof_acceptance",
        "scope_id",
    ],
)
def test_scope_membership_or_approval_tampering_is_rejected(prepared, field):
    root, base = prepared
    record = _checked(root, base, phase="representative_slice")["record"]
    bind_review_scope(root, record)
    record["controller_review_scope"][field] = (
        True if field.endswith("authority") or field == "proof_acceptance" else []
    )
    with pytest.raises(ValueError, match="review scope"):
        review_scope(record)


def test_missing_scope_or_changed_checked_plan_blocks_before_review(prepared, monkeypatch):
    root, base = prepared
    record = _checked(root, base, phase="representative_slice")["record"]
    with pytest.raises(ValueError, match="scope is missing"):
        mandatory_dossier(record, record["units"], coherence=True)
    original = preparation.load_preparation_receipt(root)
    changed = copy.deepcopy(original)
    changed["plan"]["slice_source_ids"] = base["slice_source_ids"][:1]
    monkeypatch.setattr(preparation, "load_preparation_receipt", lambda _: changed)
    with pytest.raises(ValueError, match="preparation plan changed"):
        bind_review_scope(root, record)


def test_paged_assignment_retains_bounded_inline_phase_and_scope_references(prepared):
    root, base = prepared
    record = _checked(root, base, phase="representative_slice")["record"]
    bind_review_scope(root, record)
    record["units"][0]["representation_note"] *= 3000
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    assignment, docs = review_assignment(record, dossier, "current-dossier")
    assert assignment["dossier_ref"] == "assignment-dossier"
    assert len(json.dumps(assignment).encode()) < 4000
    header = assignment["controller_scope"]
    assert header["proposal_phase"] == "representative_slice"
    assert header["scope_evidence_ref"] == "controller-scope"
    assert header["full_inventory_ref"] == "full-inventory"
    assert docs["controller-scope"]["scope_id"] == header["scope_id"]
    assert docs["full-inventory"] == base["inventory"]


def test_rejection_cache_binds_exact_controller_authority_and_protocol(prepared):
    root, base = prepared
    record = _checked(root, base, phase="representative_slice")["record"]
    bind_review_scope(root, record)
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    config = {"protocol": review.PROTOCOL}
    first = rejection_key(record, dossier, config)
    altered = copy.deepcopy(dossier)
    altered["controller_scope"]["scope_id"] = "other-controller-authority"
    assert rejection_key(record, altered, config) != first
    assert rejection_key(record, dossier, {"protocol": "source-review-v7"}) != first
    assert "temporarily deferred" in review._CONTRACT


def test_assignment_cannot_replace_frozen_header_with_author_claim(prepared):
    root, base = prepared
    record = _checked(root, base, phase="representative_slice")["record"]
    bind_review_scope(root, record)
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    dossier["controller_scope"]["publication_authority"] = True
    with pytest.raises(ValueError, match="frozen controller scope"):
        review_assignment(record, dossier, "forged-authority")


def test_legacy_author_slice_claim_never_grants_deferral(prepared):
    root, base = prepared
    record = _checked(root, base, phase="complete")["record"]
    record.pop("workflow_protocol")
    record["proposal_phase"] = "representative_slice"
    record["controller_review_scope"] = {
        "proposal_phase": "representative_slice",
        "proof_acceptance": True,
    }
    scope = review_scope(record)
    assert scope["proposal_phase"] == "complete"
    assert scope["workflow_protocol"] is None
    assert scope["checked_preparation"] is None
    assert scope["remaining_planned_source_ids"] == scope["remaining_planned_region_ids"] == []
