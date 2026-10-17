"""Expose controller-selected formalization stages without publication or source-write authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, durable_json, identity
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.preparation_assignment import checked_preparation_assignment
from leanflow_cli.formalization.preparation_campaign import (
    campaign_budget,
    load_campaign,
    select_campaign,
)
from leanflow_cli.formalization.records import current_selection_token, load_document
from leanflow_cli.formalization.slice_gate import protocol_enabled, slice_regions, slice_status
from leanflow_cli.formalization.source_review_regions import coalesce_regions, compact_region


def campaign_budget_path(root: Path, environment: dict[str, str], directory: Path) -> Path:
    """Require run-bound accounting and independently validate any prepared-budget hint."""
    run_id = (
        environment.get("LEANFLOW_NATIVE_RESUME_RUN_ID")
        or environment.get("LEANFLOW_WORKFLOW_RUN_ID")
        or directory.name
    )
    record = load_document(root, environment["LEANFLOW_FORMALIZATION_DOCUMENT_ID"])
    resume = bool(environment.get("LEANFLOW_NATIVE_RESUME_RUN_ID"))
    binding_path = directory / "campaign-accounting.json"
    existing = load_campaign(root, run_id) if resume or binding_path.exists() else None
    limits = {
        name: environment.get(
            "LEANFLOW_PROVER_" + name.upper(), (existing or {}).get("limits", {}).get(name)
        )
        for name in ("agent_calls", "tokens", "wall_time_s")
    }
    from leanflow_cli.formalization.intake import draft_source_rejection

    binding = select_campaign(
        root,
        record,
        run_id=run_id,
        limits=limits,
        backend=environment.get("LEANFLOW_NATIVE_PROVIDER", "codex"),
        selection_token=current_selection_token(root, record["document_id"]),
        source_draft_only=environment.get("LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY", "false")
        == "true",
        effective_draft=draft_source_rejection(
            complete_inventory(record),
            source_draft_only=environment.get("LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY", "false")
            == "true",
        )
        is not None,
        resume=existing is not None,
        budget_path=None if existing is not None else directory / "limits.json",
    )
    bound_path = campaign_budget(root, binding)
    value = environment.get("LEANFLOW_FORMALIZATION_BUDGET_PATH", "")
    if not value:
        return bound_path
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
        or requested != bound_path
    ):
        raise ValueError("Shared formalization budget differs from the prepared campaign")
    prepared_record = load_document(
        root,
        environment["LEANFLOW_FORMALIZATION_DOCUMENT_ID"],
        environment["LEANFLOW_FORMALIZATION_REVISION"],
    )
    if prepared_record.get("preparation_id") != receipt["preparation_id"]:
        raise ValueError("Shared campaign preparation does not match the frozen source")
    return requested


def campaign_budget_limits(path: Path, environment: dict[str, str]) -> dict[str, Any]:
    """Retain prepared campaign defaults while rejecting incompatible explicit overrides."""
    names = ("agent_calls", "tokens", "wall_time_s")
    saved: dict[str, Any] = {}
    if path.exists():
        value = json.loads(path.read_text())
        if value.get("version") != 2 or set(value.get("limits", {})) != set(names):
            raise ValueError("Shared formalization campaign limits are malformed")
        saved = value["limits"]
    return {
        name: environment.get("LEANFLOW_PROVER_" + name.upper(), saved.get(name)) for name in names
    }


def stage_assignment(
    root: Path,
    environment: dict[str, str],
    context: dict[str, Any],
    prompt: str,
    *,
    best_effort: bool = False,
) -> tuple[dict[str, Any], str]:
    """Describe only the currently admitted slice or full expansion from durable controller state."""
    record = load_document(
        root,
        environment["LEANFLOW_FORMALIZATION_DOCUMENT_ID"],
        environment["LEANFLOW_FORMALIZATION_REVISION"],
    )
    if not protocol_enabled(record):
        return context, prompt
    stage = (
        {"phase": "complete", "required": True, "best_effort": True, "slice_id": None}
        if best_effort
        else slice_status(root, record)
    )
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
    selected = slice_regions(record) if stage["phase"] == "representative_slice" else None
    regions = [row for row in inventory["regions"] if selected is None or row["id"] in selected]
    groups = coalesce_regions(regions)
    partition = {
        "phase": stage["phase"],
        "canonical_regions": regions,
        "groups": groups,
        "source_bundle": record["source_bundle"],
        "base_revision_id": record["revision_id"],
        "authority": "exact selected canonical source denominator; no source approval",
        **({"full_inventory": inventory} if not plan_ref["manifest"] else {}),
    }
    partition_reference: dict[str, Any] = {
        "sha256": identity(partition),
        "canonical_region_count": len(regions),
        "group_count": len(groups),
    }
    if context.get("project_root"):
        path = contained(
            Path(context["project_root"]), ".leanflow-job/formalization-stage-partition.json"
        )
        durable_json(path, partition)
        partition_reference["path"] = str(path)
    else:
        context["source_region_partition"] = {**partition, "full_inventory": inventory}
        partition_reference["assignment_field"] = "source_region_partition"
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
        "required_region_ids": [group["id"] for group in groups],
        "source_region_partition": partition_reference,
        "regions": [compact_region(group) for group in groups],
        "publication_authority": stage["phase"] == "complete" and not best_effort,
        "proof_authority": False,
    }
    context["formalization_stage"] = assignment
    if best_effort:
        return context, prompt + (
            "\nComplete the best-effort formalization of the ENTIRE paper now.\n"
            + json.dumps(assignment, ensure_ascii=False)
            + "\nHuman interpretation is optional and must never stop generation. Keep original "
            "source unchanged. Implement all definitions/data, leave theorem proofs as named "
            "sorry obligations. Choose and explicitly label provisional readings or alternative "
            "statements for ambiguous passages; record assumptions, source errors and reasons "
            "in setup-notes.md/blockers.md and the proposal. Do not mark known-false literal "
            "claims as ordinary proof work. No human permission or source-review PASS is "
            "required to FINISH THIS UNACCEPTED DRAFT. No statement approval, slice PASS or "
            "Prove handoff is granted. Cover every original assigned item, clause and region "
            "in a full phase=complete proposal; unresolved clauses remain explicitly accounted "
            "for. Finish all remaining modules, update the aggregator, use Lean tools to fix "
            "compile errors, and submit_files to report saved coverage. A response with "
            "accepted=false and draft_saved=true is expected: finish after saving the complete "
            "project. Review findings remain annotations for later human revision.\n"
        )
    instructions = (
        "\nController-enforced formalization stage:\n"
        + json.dumps(assignment, ensure_ascii=False)
        + "\nThe proposal JSON must include phase matching this assignment. "
        "Read the complete checked preparation plan from its manifest reference, including "
        "prerequisite candidate declarations and representation estimates. These estimates "
        "are not semantic approval; compile faithful reuse and review it with the source. "
        "Cover every assigned review group ID exactly once. The exact ordered canonical "
        "members remain available in source_region_partition; the full intake partition "
        "retains all original source context. Do not mix group IDs and canonical IDs. "
        "For representative_slice, map exactly its required source IDs and assigned groups. "
        "Keep original source unchanged and implement definitions fully. "
        "Use separate files for the slice so expansion can preserve them exactly. "
        "Do not translate the remaining source until the independent slice review passes. "
        "For complete, retain all slice declaration names, mappings, representations and "
        "file bytes, add remaining modules, and extend only import-only aggregators. "
        "Any changed slice returns to representative_slice and independent review. "
        "The controller reviews every phase; technical submit acceptance alone grants "
        "no source approval or publication. Proving is a later separate action.\n"
    )
    return context, prompt + instructions
