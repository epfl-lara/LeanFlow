"""Expose controller-selected formalization stages without publication or source-write authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.preparation_assignment import checked_preparation_assignment
from leanflow_cli.formalization.records import load_document
from leanflow_cli.formalization.slice_gate import protocol_enabled, slice_regions, slice_status


def campaign_budget_path(root: Path, environment: dict[str, str], directory: Path) -> Path:
    """Select a shared budget only through its controller-owned preparation receipt."""
    value = environment.get("LEANFLOW_FORMALIZATION_BUDGET_PATH", "")
    if not value:
        return directory / "limits.json"
    from leanflow_cli.formalization.preparation import load_preparation_receipt

    receipt = load_preparation_receipt(root)
    if receipt is None or not receipt.get("budget_path"):
        raise ValueError("Shared formalization budget has no controller preparation authority")
    requested = Path(value)
    requested = root / requested if not requested.is_absolute() else requested
    selected = Path(receipt["budget_path"])
    selected = root / selected if not selected.is_absolute() else selected
    requested = contained(root, requested.relative_to(root).as_posix())
    if (
        not requested.is_relative_to(root / ".leanflow/formalization-campaigns")
        or requested != selected
    ):
        raise ValueError("Shared formalization budget differs from the prepared campaign")
    record = load_document(
        root,
        environment["LEANFLOW_FORMALIZATION_DOCUMENT_ID"],
        environment["LEANFLOW_FORMALIZATION_REVISION"],
    )
    if record.get("preparation_id") != receipt["preparation_id"]:
        raise ValueError("Shared campaign preparation does not match the frozen source")
    return requested


def campaign_budget_limits(path: Path, environment: dict[str, str]) -> dict[str, Any]:
    """Retain prepared campaign defaults while rejecting incompatible explicit overrides."""
    names = ("agent_calls", "tokens", "wall_time_s")
    saved: dict[str, Any] = {}
    if environment.get("LEANFLOW_FORMALIZATION_BUDGET_PATH"):
        value = json.loads(path.read_text())
        if value.get("version") != 2 or set(value.get("limits", {})) != set(names):
            raise ValueError("Shared formalization campaign limits are malformed")
        saved = value["limits"]
    return {
        name: environment.get("LEANFLOW_PROVER_" + name.upper(), saved.get(name)) for name in names
    }


def stage_assignment(
    root: Path, environment: dict[str, str], context: dict[str, Any], prompt: str
) -> tuple[dict[str, Any], str]:
    """Describe only the currently admitted slice or full expansion from durable controller state."""
    record = load_document(
        root,
        environment["LEANFLOW_FORMALIZATION_DOCUMENT_ID"],
        environment["LEANFLOW_FORMALIZATION_REVISION"],
    )
    if not protocol_enabled(record):
        return context, prompt
    stage = slice_status(root, record)
    inventory = complete_inventory(record)
    prepared = checked_preparation_assignment(root, record)
    plan = prepared["plan"]
    plan_ref = {
        "manifest": context.get("formalization_manifest"),
        "json_pointer": "/checked_preparation_plan",
        "preparation_plan_id": record["preparation_plan_id"],
        "required_imports": plan["required_imports"],
        "prerequisite_count": len(plan["mathematical_prerequisites"]),
        "representation_note_count": len(plan["representation_notes"]),
        "mathematical_approval": prepared["mathematical_approval"],
    }
    if not plan_ref["manifest"]:
        context["checked_preparation_plan"] = prepared
        plan_ref["assignment_field"] = "checked_preparation_plan"
    assignment = {
        **stage,
        "workflow_protocol": 2,
        "preparation_id": record["preparation_id"],
        "preparation_plan_id": record["preparation_plan_id"],
        "checked_preparation_plan": plan_ref,
        "base_revision_id": record["revision_id"],
        "required_source_ids": (
            record["slice_source_ids"]
            if stage["phase"] == "representative_slice"
            else [item["id"] for item in inventory["items"] if item.get("scope") != "excluded"]
        ),
        "required_region_ids": (
            sorted(slice_regions(record))
            if stage["phase"] == "representative_slice"
            else [region["id"] for region in inventory["regions"]]
        ),
        "publication_authority": stage["phase"] == "complete",
    }
    context["formalization_stage"] = assignment
    instructions = (
        "\nController-enforced formalization stage:\n"
        + json.dumps(assignment, ensure_ascii=False)
        + "\nThe proposal JSON must include phase matching this assignment. "
        "Read the complete checked preparation plan from its manifest reference, including "
        "prerequisite candidate declarations and representation estimates. These estimates "
        "are not semantic approval; compile faithful reuse and review it with the source. "
        "For representative_slice, map exactly its required source IDs and coverage "
        "regions. Keep original source unchanged and implement definitions fully. "
        "Use separate files for the slice so expansion can preserve them exactly. "
        "Do not translate the remaining source until the independent slice review passes. "
        "For complete, retain all slice declaration names, mappings, representations and "
        "file bytes, add remaining modules, and extend only import-only aggregators. "
        "Any changed slice returns to representative_slice and independent review. "
        "The controller reviews every phase; technical submit acceptance alone grants "
        "no source approval or publication. Proving is a later separate action.\n"
    )
    return context, prompt + instructions
