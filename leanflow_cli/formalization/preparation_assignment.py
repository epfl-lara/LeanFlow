"""Deliver receipt-bound mathematical estimates without granting semantic or acquisition approval."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.stage_errors import RepreparationRequired


def checked_preparation_assignment(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Expose the exact checked setup plan and label its mathematical estimates separately."""
    from leanflow_cli.formalization.preparation import load_preparation_receipt

    try:
        receipt = load_preparation_receipt(root)
    except (OSError, ValueError, RuntimeError) as error:
        raise RepreparationRequired(str(error)) from error
    plan = (receipt or {}).get("plan", {})
    included = {
        row["id"] for row in complete_inventory(record)["items"] if row.get("scope") != "excluded"
    }
    if (
        receipt is None
        or receipt.get("preparation_id") != record["preparation_id"]
        or receipt.get("preparation_plan_id") != record["preparation_plan_id"]
        or receipt.get("source_bundle") != record["source_bundle"]
        or plan.get("plan_id") != record["preparation_plan_id"]
        or plan.get("slice_source_ids") != record["slice_source_ids"]
        or not set(record["slice_source_ids"]) <= included
    ):
        raise RepreparationRequired(
            "Checked preparation plan changed; prepare the selected source again"
        )
    return {
        "preparation_id": receipt["preparation_id"],
        "preparation_plan_id": receipt["preparation_plan_id"],
        "plan": copy.deepcopy(plan),
        "infrastructure_status": "checked",
        "mathematical_approval": "none; estimates require compiled translation, representative slice and independent source review",
        "acquisition_authority": "Only the receipt's existing pins, approved catalog and explicit user dependency plan authorize packages",
    }
