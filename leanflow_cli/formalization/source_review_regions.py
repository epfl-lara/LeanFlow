"""Group contiguous source census members without narrowing canonical coverage authority."""

from __future__ import annotations

import copy
from typing import Any

from .evidence import identity

MAX_GROUP_BYTES = 16384
MAX_GROUP_MEMBERS = 256


def compact_region(group: dict[str, Any]) -> dict[str, Any]:
    """Expose an exact partition summary while keeping the full census in frozen evidence."""
    result = {
        key: copy.deepcopy(value)
        for key, value in group.items()
        if key not in {"member_regions", "member_region_ids"}
    }
    result.update(
        member_count=len(group["member_region_ids"]),
        partition_evidence_id="source-region-partition",
    )
    return result


def _boundary(region: dict[str, Any]) -> dict[str, Any]:
    """Keep occurrences, detected mathematics, exclusions and repair boundaries distinct."""
    return {
        key: region.get(key)
        for key in (
            "file",
            "file_hash",
            "coordinate_space",
            "occurrence_id",
            "page_index",
            "item_ids",
            "repair_required",
            "exclusion_kind",
        )
    } | {
        "excluded": region.get("disposition") == "excluded",
        "exclusion_actor": (
            region.get("actor") if region.get("disposition") == "excluded" else None
        ),
    }


def coalesce_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build bounded review partitions while retaining every exact original member record.

    A single preexisting large census member remains whole and uses evidence pagination;
    the grouping operation never invents new byte boundaries or truncates its contents.
    """
    ids = [row["id"] for row in regions]
    if len(set(ids)) != len(ids):
        raise ValueError("Canonical source regions must have unique identities")
    pending: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []

    def flush() -> None:
        """Bind a group identity to its full ordered original records, not just coordinates."""
        if not pending:
            return
        members = copy.deepcopy(pending)
        first, last = members[0], members[-1]
        group = copy.deepcopy(first)
        group.update(
            id="review-region-" + identity(members),
            member_region_ids=[row["id"] for row in members],
            member_regions=members,
            membership_sha256=identity(members),
            region_authority="exact-contiguous-canonical-partition",
        )
        if len(members) > 1:
            group["byte_end"] = last["byte_end"]
            if len({row.get("disposition") for row in members}) != 1:
                group["disposition"] = "unresolved"
            group["reason"] = "Review every original member, including all source uncertainty"
        result.append(group)
        pending.clear()

    for row in regions:
        previous = pending[-1] if pending else None
        join = (
            previous is not None
            and row.get("coordinate_space") == "source_bytes"
            and previous.get("byte_end") == row.get("byte_start")
            and _boundary(previous) == _boundary(row)
            and len(pending) < MAX_GROUP_MEMBERS
            and row["byte_end"] - pending[0]["byte_start"] <= MAX_GROUP_BYTES
        )
        if not join:
            flush()
        pending.append(row)
    flush()
    return result


def _rows_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reject malformed or duplicate proposal/review row identities before expansion."""
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("region_id"), str) for row in rows
    ):
        raise ValueError("Source coverage rows require exact region identities")
    result = {row["region_id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("Source coverage contains duplicate region identities")
    return result


def expand_coverage(
    regions: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Expand an exact complete group proposal before the ordinary token coverage checks."""
    supplied = _rows_by_id(rows)
    canonical = {row["id"] for row in regions}
    if set(supplied) == canonical:
        return copy.deepcopy(rows)
    groups = coalesce_regions(regions)
    if set(supplied) != {group["id"] for group in groups}:
        raise ValueError("Coverage must name every exact canonical region or review group")
    expanded = []
    for group in groups:
        row = supplied[group["id"]]
        if "member_region_ids" in row and row["member_region_ids"] != group["member_region_ids"]:
            raise ValueError("Coverage group membership differs from the frozen source census")
        for key in group["member_region_ids"]:
            expanded.append(
                {
                    **copy.deepcopy(row),
                    "region_id": key,
                    "review_group_id": group["id"],
                }
            )
    return expanded


def expand_review_coverage(
    regions: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    assigned_group_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Expand only exact current assigned group verdicts into canonical receipt coverage."""
    groups = {group["id"]: group for group in coalesce_regions(regions)}
    assigned = list(groups) if assigned_group_ids is None else assigned_group_ids
    if len(set(assigned)) != len(assigned) or set(assigned) - set(groups):
        raise ValueError("Review assignment contains stale or duplicate source groups")
    supplied = _rows_by_id(rows)
    if set(supplied) != set(assigned):
        raise ValueError("Review omitted or invented assigned source groups")
    expanded = []
    for key in assigned:
        row = supplied[key]
        if (
            set(row) != {"region_id", "decision", "rationale"}
            or row["decision"] not in {"PASS", "BLOCK"}
            or not isinstance(row["rationale"], str)
            or not row["rationale"].strip()
        ):
            raise ValueError("Every source group requires a specific PASS or BLOCK assessment")
        for member in groups[key]["member_region_ids"]:
            expanded.append({**row, "region_id": member})
    return expanded
