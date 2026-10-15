"""Pin exact contiguous census partitions and refuse stale or partial grouped authority."""

from __future__ import annotations

import copy

import pytest

from leanflow_cli.formalization.source_review_regions import (
    MAX_GROUP_BYTES,
    MAX_GROUP_MEMBERS,
    coalesce_regions,
    compact_region,
    expand_coverage,
    expand_review_coverage,
)


def regions(count: int = 10) -> list[dict]:
    return [
        {
            "id": f"region-{index}",
            "file": "paper.tex",
            "file_hash": "source-hash",
            "occurrence_id": "occurrence-1",
            "coordinate_space": "source_bytes",
            "byte_start": index * 3,
            "byte_end": (index + 1) * 3,
            "disposition": "unresolved" if index % 2 else "non_mathematical",
            "item_ids": [],
            "reason": "Unclassified token" if index % 2 else "Whitespace",
        }
        for index in range(count)
    ]


def test_groups_preserve_every_original_member_and_exact_bounds() -> None:
    original = regions(9050)
    before = copy.deepcopy(original)
    groups = coalesce_regions(original)
    assert len(groups) == 36
    assert [row for group in groups for row in group["member_regions"]] == original
    assert [key for group in groups for key in group["member_region_ids"]] == [
        row["id"] for row in original
    ]
    assert original == before
    assert all(len(group["member_region_ids"]) <= MAX_GROUP_MEMBERS for group in groups)
    assert all(group["byte_end"] - group["byte_start"] <= MAX_GROUP_BYTES for group in groups)


def test_compact_group_summary_keeps_exact_identity_and_points_to_full_partition() -> None:
    group = coalesce_regions(regions())[0]
    summary = compact_region(group)
    assert summary["id"] == group["id"]
    assert summary["membership_sha256"] == group["membership_sha256"]
    assert summary["member_count"] == len(group["member_region_ids"])
    assert summary["partition_evidence_id"] == "source-region-partition"
    assert "member_regions" not in summary and "member_region_ids" not in summary
    assert group["member_regions"] == regions()


@pytest.mark.parametrize(
    "change",
    [
        {"file": "child.tex"},
        {"file_hash": "different"},
        {"occurrence_id": "occurrence-2"},
        {"item_ids": ["theorem"]},
        {"repair_required": True},
        {"disposition": "excluded", "actor": "source", "exclusion_kind": "inactive"},
        {"byte_start": 4},
        {"coordinate_space": "pdf_page", "page_index": 0},
    ],
)
def test_groups_never_bridge_occurrences_gaps_repairs_or_exclusions(change: dict) -> None:
    original = regions(2)
    original[1].update(change)
    assert len(coalesce_regions(original)) == 2


def test_group_bytes_are_bounded_without_splitting_large_original_tokens() -> None:
    original = regions(2)
    original[0]["byte_end"] = MAX_GROUP_BYTES + 1
    original[1].update(byte_start=MAX_GROUP_BYTES + 1, byte_end=MAX_GROUP_BYTES + 3)
    groups = coalesce_regions(original)
    assert len(groups) == 2
    assert groups[0]["member_regions"] == original[:1]


def test_group_proposal_expands_before_the_existing_canonical_checks() -> None:
    original = regions()
    groups = coalesce_regions(original)
    rows = [
        {
            "region_id": group["id"],
            "disposition": "supporting_context",
            "item_ids": [],
            "reason": "Read every exact member as source context",
        }
        for group in groups
    ]
    expanded = expand_coverage(original, rows)
    assert [row["region_id"] for row in expanded] == [row["id"] for row in original]
    assert all(row["review_group_id"] == groups[0]["id"] for row in expanded)
    assert expand_coverage(original, expanded) == expanded


@pytest.mark.parametrize("tamper", ["source", "span", "member", "mixed", "duplicate", "partial"])
def test_proposal_group_membership_cannot_be_stale_or_incomplete(tamper: str) -> None:
    original = regions()
    groups = coalesce_regions(original)
    rows = [{"region_id": groups[0]["id"]}]
    if tamper == "source":
        original[0]["file_hash"] = "replacement"
    elif tamper == "span":
        original[0]["byte_end"] -= 1
    elif tamper == "member":
        rows[0]["member_region_ids"] = groups[0]["member_region_ids"][:-1]
    elif tamper == "mixed":
        rows.append({"region_id": original[0]["id"]})
    elif tamper == "duplicate":
        rows *= 2
    else:
        rows.clear()
    with pytest.raises(ValueError):
        expand_coverage(original, rows)


def test_only_exact_assigned_positive_group_verdicts_expand_to_token_passes() -> None:
    original = regions(MAX_GROUP_MEMBERS + 1)
    groups = coalesce_regions(original)
    rows = [{"region_id": groups[0]["id"], "decision": "PASS", "rationale": "All members read"}]
    result = expand_review_coverage(original, rows, assigned_group_ids=[groups[0]["id"]])
    assert len(result) == MAX_GROUP_MEMBERS
    assert {row["region_id"] for row in result} == set(groups[0]["member_region_ids"])
    assert all(row["decision"] == "PASS" for row in result)
    with pytest.raises(ValueError):
        expand_review_coverage(original, rows)
    original[0]["reason"] = "Source interpretation changed"
    with pytest.raises(ValueError):
        expand_review_coverage(original, rows, assigned_group_ids=[groups[0]["id"]])


@pytest.mark.parametrize("decision", ["UNAVAILABLE", None, True, ""])
def test_no_unknown_review_result_can_create_canonical_pass(decision: object) -> None:
    original = regions()
    group = coalesce_regions(original)[0]
    with pytest.raises(ValueError):
        expand_review_coverage(
            original, [{"region_id": group["id"], "decision": decision, "rationale": "Evidence"}]
        )


def test_duplicate_census_ids_are_rejected_before_partitioning() -> None:
    original = regions()
    original[-1]["id"] = original[0]["id"]
    with pytest.raises(ValueError):
        coalesce_regions(original)
