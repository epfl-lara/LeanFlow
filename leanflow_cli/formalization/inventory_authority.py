"""Keep complete controller source authority separate from phase-specific review projections."""

from __future__ import annotations

import copy
from typing import Any


def complete_inventory(record: dict[str, Any]) -> dict[str, Any]:
    """Return the complete source/user inventory rather than a filtered slice view."""
    return record.get("full_inventory", record["inventory"])


def select_review_inventory(record: dict[str, Any]) -> None:
    """Project selected regions after repairs while retaining complete canonical evidence."""
    record["inventory"] = copy.deepcopy(complete_inventory(record))
    if (
        record.get("workflow_protocol") == 2
        and record.get("proposal_phase") == "representative_slice"
    ):
        from leanflow_cli.formalization.slice_gate import slice_regions

        selected = slice_regions(record)
        record["inventory"]["regions"] = [
            row for row in record["inventory"]["regions"] if row["id"] in selected
        ]
        record["inventory"]["slice_audit_complete"] = False


def display_inventory(record: dict[str, Any], *, reviewed: bool) -> dict[str, Any]:
    """Show reviewed whole-document coverage only after validating its complete source denominator."""
    canonical = complete_inventory(record)
    if (
        not reviewed
        or record.get("workflow_protocol") != 2
        or record.get("proposal_phase") != "complete"
    ):
        return canonical
    try:
        assert_complete_coverage(record)
    except (KeyError, TypeError, ValueError):
        return canonical
    return record["inventory"]


def assert_complete_coverage(record: dict[str, Any]) -> None:
    """Reject protocol2 publication/proof authority over narrowed or unresolved source coverage."""
    if record.get("workflow_protocol") != 2:
        return
    canonical, reviewed = complete_inventory(record), record["inventory"]
    error = "Complete source coverage must include every canonical item and region"
    if (
        record.get("proposal_phase") != "complete"
        or reviewed.get("audit_complete") is not True
        or reviewed["items"] != canonical["items"]
    ):
        raise ValueError(error)
    before = {row["id"]: row for row in canonical["regions"]}
    after = {row["id"]: row for row in reviewed["regions"]}
    if (
        len(before) != len(canonical["regions"])
        or len(after) != len(reviewed["regions"])
        or set(before) != set(after)
    ):
        raise ValueError(error)
    mutable = {"disposition", "item_ids", "reason", "actor"}
    items = {item["id"]: item for item in canonical["items"]}
    for key, region in after.items():
        original = before[key]
        linked = region.get("item_ids", [])
        if (
            {name: value for name, value in region.items() if name not in mutable}
            != {name: value for name, value in original.items() if name not in mutable}
            or region.get("repair_required")
            or region.get("disposition")
            not in {"item", "supporting_context", "non_mathematical", "excluded"}
        ):
            raise ValueError(error)
        if (
            not isinstance(linked, list)
            or any(item_id not in items for item_id in linked)
            or (region["disposition"] == "item" and not linked)
            or not isinstance(region.get("reason"), str)
            or not region["reason"].strip()
        ):
            raise ValueError(error)
        if region["disposition"] == "excluded" and not (
            original.get("disposition") == "excluded"
            and original.get("actor") in {"user", "source"}
        ):
            raise ValueError("Complete source coverage cannot invent exclusions")
        if (
            original.get("item_ids")
            and region["disposition"] != "item"
            and not (
                region["disposition"] == "excluded"
                and original.get("actor") == "user"
                and all(
                    items[item_id].get("scope") == "excluded" for item_id in original["item_ids"]
                )
            )
        ):
            raise ValueError(
                "Complete source coverage cannot silently reclassify detected mathematics"
            )
    required = {item["id"] for item in canonical["items"] if item.get("scope") != "excluded"}
    mapped = {key for unit in record["units"] for key in unit["required_source_ids"]}
    if required - mapped:
        raise ValueError(error)
