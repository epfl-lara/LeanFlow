"""Expand reviewed groups only through their exact unmodified canonical source partition."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import review, review_cache
from leanflow_cli.formalization.proposal_schema import apply_proposal
from leanflow_cli.formalization.records import create_document
from leanflow_cli.formalization.review_dossier import evidence_documents, mandatory_dossier
from leanflow_cli.formalization.source_reconciliation import (
    assert_reconciliation_reviewed,
    reconciliation_requirements,
)
from leanflow_cli.formalization.source_review_regions import coalesce_regions
from tests.leanflow import test_formalization_revision_review as review_helpers
from tests.leanflow.test_formalization_revision_review import answer
from tests.leanflow.test_formalization_revision_review import source_record as _source_record

source_record = _source_record


def test_compact_review_keeps_full_partition_and_emits_canonical_coverage(source_record) -> None:
    before = copy.deepcopy(source_record["inventory"])
    documents = evidence_documents(source_record)
    dossier = mandatory_dossier(
        source_record, source_record["units"], coherence=True, documents=documents
    )
    assert all(
        "member_regions" not in row and "member_region_ids" not in row for row in dossier["regions"]
    )
    partition = documents["source-region-partition"]
    assert [row for group in partition["groups"] for row in group["member_regions"]] == before[
        "regions"
    ]
    result = review.validate_review(
        answer(dossier, "revision", "dossier"),
        dossier,
        "revision",
        "dossier",
        canonical_regions=before["regions"],
    )
    assert result["accepted"]
    assert [row["region_id"] for row in result["coverage"]] == [
        row["id"] for row in before["regions"]
    ]
    assert [row["region_id"] for row in result["coverage_groups"]] == [
        row["id"] for row in dossier["regions"]
    ]
    assert source_record["inventory"] == before


@pytest.mark.parametrize(
    "mutation",
    [
        "member_count",
        "membership_hash",
        "span",
        "group_id",
        "partition_hash",
        "omitted_group",
        "canonical_response",
        "changed_canonical",
    ],
)
def test_forged_or_incomplete_review_partition_cannot_receive_pass(source_record, mutation) -> None:
    regions = copy.deepcopy(source_record["inventory"]["regions"])
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    if mutation == "member_count":
        dossier["regions"][0]["member_count"] -= 1
    elif mutation == "membership_hash":
        dossier["regions"][0]["membership_sha256"] = "forged"
    elif mutation == "span":
        dossier["regions"][0]["byte_end"] -= 1
    elif mutation == "group_id":
        dossier["regions"][0]["id"] = "invented-group"
    elif mutation == "partition_hash":
        dossier["coverage_partition"]["membership_sha256"] = "forged"
    elif mutation == "omitted_group":
        dossier["regions"].pop()
    elif mutation == "changed_canonical":
        regions[-1]["byte_end"] += 1
    value = json.loads(answer(dossier, "revision", "dossier"))
    if mutation == "canonical_response":
        value["coverage"][0]["region_id"] = regions[0]["id"]
    with pytest.raises(ValueError):
        review.validate_review(
            json.dumps(value), dossier, "revision", "dossier", canonical_regions=regions
        )


def test_compact_review_cannot_claim_canonical_acceptance_without_controller_context(
    source_record,
) -> None:
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    with pytest.raises(ValueError, match="canonical controller coverage"):
        review.validate_review(
            answer(dossier, "revision", "dossier"), dossier, "revision", "dossier"
        )


def _lexical_proposal(source_record, tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(
        r"\documentclass{article}\begin{document}"
        r"\begin{theorem}\label{thm:t}True.\end{theorem}\end{document}"
    )
    record = create_document(tmp_path, source)
    for key in ("units", "layout", "environment_id", "lean_files", "checks"):
        record[key] = copy.deepcopy(source_record[key])
    record["units"][0]["required_source_ids"] = [row["id"] for row in record["inventory"]["items"]]
    requirements = reconciliation_requirements(record)
    assert requirements["diagnostics"]
    proposal = {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "units": record["units"],
        "coverage": [
            {
                "region_id": group["id"],
                "item_ids": group.get("item_ids", []),
                "disposition": "item" if group.get("item_ids") else "supporting_context",
                "reason": "Preserve every exact contiguous source member.",
            }
            for group in coalesce_regions(record["inventory"]["regions"])
        ],
        "source_reconciliation": {
            "requirements_id": requirements["requirements_id"],
            "entries": [
                {
                    "diagnostic_id": row["diagnostic_id"],
                    "interpretation": "Read the complete literal source; the package cannot certify extraction.",
                    "reason": "Independent region and coherence reviews must check this interpretation.",
                }
                for row in requirements["diagnostics"]
            ],
        },
    }
    return record, proposal


def test_grouped_proposal_requires_fresh_complete_reconciliation_reviews(
    source_record, tmp_path, monkeypatch
) -> None:
    record, proposal = _lexical_proposal(source_record, tmp_path)
    before = copy.deepcopy(record["inventory"])
    candidate = apply_proposal(record, proposal)
    assert candidate["full_inventory"] == before
    assert [row["id"] for row in candidate["inventory"]["regions"]] == [
        row["id"] for row in before["regions"]
    ]
    assert candidate["source_reconciliation"]["verified"] is False
    with pytest.raises(ValueError, match="fresh independently"):
        assert_reconciliation_reviewed(candidate, [])
    calls, kwargs = review_helpers._review_transport(candidate, tmp_path, monkeypatch)
    outcome = review.review_revision(candidate, **kwargs)
    assert outcome["accepted"] and len(calls) == 2
    assert_reconciliation_reviewed(candidate, outcome["reviews"])
    assert candidate["full_inventory"] == before
    assert candidate["source_reconciliation"]["verified"] is False
    assert all(row["blocking"] for row in before["diagnostics"] if row.get("blocking"))
    incomplete = copy.deepcopy(outcome["reviews"])
    next(row for row in incomplete if row["coverage"])["coverage"].pop()
    with pytest.raises(ValueError, match="complete canonical"):
        assert_reconciliation_reviewed(candidate, incomplete)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "invented", "stale"])
def test_grouped_proposal_cannot_skip_or_reuse_lexical_uncertainty(
    source_record, tmp_path, mutation
) -> None:
    record, proposal = _lexical_proposal(source_record, tmp_path)
    reading = proposal["source_reconciliation"]
    if mutation == "missing":
        reading["entries"].pop()
    elif mutation == "duplicate":
        reading["entries"].append(copy.deepcopy(reading["entries"][0]))
    elif mutation == "invented":
        reading["entries"][0]["diagnostic_id"] = "invented"
    else:
        reading["requirements_id"] = "previous-source-reading"
    with pytest.raises(ValueError, match="exact source reconciliation"):
        apply_proposal(record, proposal)
    assert all(row.get("blocking") for row in record["inventory"]["diagnostics"])


def test_corrected_lexical_interpretation_changes_semantic_rejection_key(
    source_record, tmp_path
) -> None:
    record, proposal = _lexical_proposal(source_record, tmp_path)
    first = apply_proposal(record, proposal)
    proposal["source_reconciliation"]["entries"][0][
        "interpretation"
    ] = "Corrected independent reading retains a previously misunderstood source convention."
    second = apply_proposal(record, proposal)
    assert first["inventory"] == second["inventory"]
    configuration = {"model": "fixture", "protocol": review.PROTOCOL}
    dossiers = [
        mandatory_dossier(value, value["units"], coherence=True) for value in (first, second)
    ]
    assert review_cache.rejection_key(
        first, dossiers[0], configuration
    ) != review_cache.rejection_key(second, dossiers[1], configuration)


def test_cached_pass_with_missing_canonical_member_cannot_complete_review(
    source_record, tmp_path, monkeypatch
) -> None:
    calls, kwargs = review_helpers._review_transport(source_record, tmp_path, monkeypatch)
    assert review.review_revision(source_record, **kwargs)["accepted"]
    count = len(calls)
    for path in (Path(source_record["_directory"]) / "review-cache").glob("*.json"):
        value = json.loads(path.read_text())
        if value.get("coverage"):
            value["coverage"].pop()
            path.write_text(json.dumps(value))
    outcome = review.review_revision(source_record, **kwargs)
    assert not outcome["accepted"] and outcome["status"] == "invalid_review"
    assert len(calls) == count
