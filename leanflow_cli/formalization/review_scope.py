"""Freeze checked controller workflow scope separately from author notes and semantic approval."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.preparation_assignment import checked_preparation_assignment
from leanflow_cli.formalization.slice_gate import protocol_enabled, slice_regions


def _ids(rows: Any) -> list[str]:
    """Require exact unique membership before describing controller-selected coverage."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Controller review scope requires a complete source inventory")
    names = [row.get("id") for row in rows]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(
        names
    ):
        raise ValueError("Controller review scope has missing or duplicate source identities")
    return sorted(names)


def _scope(record: dict[str, Any], prepared: dict[str, Any] | None) -> dict[str, Any]:
    """Bind exact mathematical coverage to protected record and checked plan fields."""
    full = complete_inventory(record)
    all_sources, all_regions = _ids(full["items"]), _ids(full["regions"])
    included = sorted(item["id"] for item in full["items"] if item.get("scope") != "excluded")
    enabled = protocol_enabled(record)
    phase = record.get("proposal_phase", "complete") if enabled else "complete"
    if phase not in {"representative_slice", "complete"}:
        raise ValueError("Controller review scope has an unsupported proposal phase")
    if enabled:
        if record.get("checks", {}).get("accepted") is not True or prepared is None:
            raise ValueError(
                "Controller review scope requires its checked candidate and preparation"
            )
        plan = prepared.get("plan", {})
        selected = record.get("slice_source_ids")
        if (
            not isinstance(selected, list)
            or not selected
            or any(not isinstance(name, str) for name in selected)
            or len(set(selected)) != len(selected)
            or not set(selected) <= set(included)
            or prepared.get("preparation_id") != record.get("preparation_id")
            or prepared.get("preparation_plan_id") != record.get("preparation_plan_id")
            or prepared.get("infrastructure_status") != "checked"
            or plan.get("plan_id") != record.get("preparation_plan_id")
            or plan.get("slice_source_ids") != selected
        ):
            raise ValueError("Controller review scope differs from its checked preparation plan")
        for key in ("preparation_id", "preparation_plan_id"):
            if not isinstance(record.get(key), str) or not record[key]:
                raise ValueError("Controller review scope lacks checked preparation identity")
    required_sources = (
        sorted(record["slice_source_ids"]) if phase == "representative_slice" else included
    )
    required_regions = (
        sorted(slice_regions(record)) if phase == "representative_slice" else all_regions
    )
    if _ids(record["inventory"]["regions"]) != required_regions:
        raise ValueError("Controller review scope differs from the candidate's coverage regions")
    remaining_sources = sorted(set(included) - set(required_sources))
    remaining_regions = sorted(set(all_regions) - set(required_regions))
    result = {
        "schema_version": 1,
        "authority": (
            "controller-checked-workflow" if enabled else "controller-full-document-review"
        ),
        "document_id": record["document_id"],
        "source_bundle": record["source_bundle"],
        "source_files": copy.deepcopy(record["source_files"]),
        "environment_id": record["environment_id"],
        "workflow_protocol": 2 if enabled else None,
        "proposal_phase": phase,
        "preparation_id": record.get("preparation_id") if enabled else None,
        "preparation_plan_id": record.get("preparation_plan_id") if enabled else None,
        "checked_preparation": copy.deepcopy(prepared) if enabled else None,
        "checked_preparation_sha256": identity(prepared) if enabled else None,
        "full_inventory_sha256": identity(
            {
                key: value
                for key, value in full.items()
                if key not in {"audit_complete", "slice_audit_complete"}
            }
        ),
        "full_source_ids": all_sources,
        "full_region_ids": all_regions,
        "included_source_ids": included,
        "required_source_ids": required_sources,
        "required_region_ids": required_regions,
        "remaining_planned_source_ids": remaining_sources,
        "remaining_planned_region_ids": remaining_regions,
        "deferral": "requires-complete-expansion" if phase == "representative_slice" else "none",
        "permanent_exclusion_authority": False,
        "publication_authority": False,
        "proof_acceptance": False,
        "mathematical_approval": "none; independent translation and coherence verdicts are still required",
        "scope_evidence_ref": "controller-scope",
        "full_inventory_ref": "full-inventory",
    }
    result["scope_id"] = "controller-scope-" + identity(result)
    return result


def bind_review_scope(root: Path, record: dict[str, Any]) -> None:
    """Stamp checked plan authority before saving a kernel-checked immutable candidate.

    Call within the existing proposal ownership after stage admission and compiler,
    source and environment checks. Author proposal fields cannot supply this stamp.
    """
    if not protocol_enabled(record):
        record.pop("controller_review_scope", None)
        return
    prepared = checked_preparation_assignment(root, record)
    record["controller_review_scope"] = _scope(record, prepared)


def review_scope(record: dict[str, Any]) -> dict[str, Any]:
    """Reject stale or missing workflow authority and ignore author-written scope claims."""
    if not protocol_enabled(record):
        return _scope(record, None)
    frozen = record.get("controller_review_scope")
    if not isinstance(frozen, dict) or not isinstance(frozen.get("checked_preparation"), dict):
        raise ValueError("Frozen controller review scope is missing; recheck the selected proposal")
    expected = _scope(record, frozen["checked_preparation"])
    if frozen != expected:
        raise ValueError("Frozen controller review scope no longer matches the selected proposal")
    return copy.deepcopy(frozen)


def scope_header(scope: dict[str, Any]) -> dict[str, Any]:
    """Keep phase and source authority readable inline even when the dossier is paged."""
    fields = (
        "scope_id",
        "authority",
        "document_id",
        "source_bundle",
        "environment_id",
        "workflow_protocol",
        "proposal_phase",
        "preparation_id",
        "preparation_plan_id",
        "checked_preparation_sha256",
        "full_inventory_sha256",
        "deferral",
        "permanent_exclusion_authority",
        "publication_authority",
        "proof_acceptance",
        "mathematical_approval",
        "scope_evidence_ref",
        "full_inventory_ref",
    )
    return {
        **{key: scope[key] for key in fields},
        "full_source_count": len(scope["full_source_ids"]),
        "full_region_count": len(scope["full_region_ids"]),
        "required_source_count": len(scope["required_source_ids"]),
        "required_region_count": len(scope["required_region_ids"]),
        "remaining_planned_source_count": len(scope["remaining_planned_source_ids"]),
        "remaining_planned_region_count": len(scope["remaining_planned_region_ids"]),
    }
