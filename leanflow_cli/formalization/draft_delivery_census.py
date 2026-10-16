"""Count authored draft mappings against the complete frozen source without approving them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.clause_obligations import obligation_registry
from leanflow_cli.formalization.evidence import contained, digest
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.source_draft_artifacts import MAX_DRAFT_FILE_BYTES, read_regular
from leanflow_cli.formalization.source_review_regions import expand_coverage

_CLAUSE_DISPOSITIONS = {
    "represented",
    "pending_proof",
    "represented_via_correction",
    "unresolved",
    "omitted",
    "specialized",
}
_REGION_DISPOSITIONS = {"item", "supporting_context", "non_mathematical", "excluded", "unresolved"}


def authored_census(snapshot: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Retain raw proposals and count exact mappings, including explicitly unresolved clauses."""
    inventory = complete_inventory(record)
    source_ids = {row["id"] for row in inventory["items"]}
    excluded = {
        row["id"]: {
            "source_id": row["id"],
            "scope": "excluded",
            "actor": row["scope_actor"],
            "reason": row["scope_reason"],
        }
        for row in inventory["items"]
        if row.get("scope") == "excluded"
        and row.get("scope_actor") == "user"
        and isinstance(row.get("scope_reason"), str)
        and row["scope_reason"].strip()
    }
    required = source_ids - set(excluded)
    regions = {row["id"] for row in inventory["regions"]}
    all_clauses = obligation_registry(record)
    excluded_clauses = {key for key, row in all_clauses.items() if row["source_id"] in excluded}
    clauses = {key: row for key, row in all_clauses.items() if key not in excluded_clauses}
    issues: list[str] = []
    proposal: dict[str, Any] = {}
    proposal_hash = ""
    try:
        data = read_regular(
            contained(snapshot, ".leanflow-job/formalization-proposal.json"), MAX_DRAFT_FILE_BYTES
        )
        proposal_hash = digest(data)
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("Draft proposal must be an object")
        proposal = raw
    except (OSError, ValueError, TypeError, RecursionError) as error:
        issues.append("Draft proposal unavailable: " + str(error))
    coverage = proposal.get("coverage", [])
    if not isinstance(coverage, list):
        issues.append("Draft coverage must be a list")
        coverage = []
    try:
        coverage = expand_coverage(inventory["regions"], coverage)
    except (ValueError, TypeError, KeyError):
        # Partial canonical rows remain useful for an honest missing-region census.
        pass
    accounted_regions: set[str] = set()
    for row in coverage:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("region_id"), str)
            or row["region_id"] not in regions
            or row["region_id"] in accounted_regions
            or not isinstance(row.get("reason"), str)
            or not row["reason"].strip()
            or not isinstance(row.get("disposition"), str)
            or row["disposition"] not in _REGION_DISPOSITIONS
            or not isinstance(row.get("item_ids", []), list)
            or not all(
                isinstance(key, str) and key in source_ids for key in row.get("item_ids", [])
            )
        ):
            issues.append("Malformed, duplicate or unknown draft coverage row")
            continue
        accounted_regions.add(row["region_id"])
    units = proposal.get("units", [])
    if not isinstance(units, list):
        issues.append("Draft translation units must be a list")
        units = []
    mapped: set[str] = set()
    accounted_clauses: set[str] = set()
    unresolved: set[str] = set()
    seen_units: set[str] = set()
    for unit in units:
        if (
            not isinstance(unit, dict)
            or not isinstance(unit.get("id"), str)
            or not unit["id"]
            or unit["id"] in seen_units
        ):
            issues.append("Draft translation unit needs a unique identity")
            continue
        seen_units.add(unit["id"])
        linked = unit.get("required_source_ids")
        declarations = unit.get("declarations")
        if (
            not isinstance(linked, list)
            or not all(isinstance(key, str) and key in required for key in linked)
            or len(set(linked)) != len(linked)
            or not isinstance(declarations, list)
            or not declarations
        ):
            issues.append("Draft unit requires exact source IDs and declaration mappings")
            continue
        names: set[str] = set()
        usable = True
        for ref in declarations:
            if (
                not isinstance(ref, dict)
                or not isinstance(ref.get("name"), str)
                or not ref["name"].strip()
            ):
                usable = False
                break
            names.add(ref["name"])
            try:
                if ref.get("file"):
                    file = ref["file"]
                    if not isinstance(file, str) or not file.endswith(".lean"):
                        raise ValueError("Draft declaration needs a Lean source file")
                    read_regular(contained(snapshot, file), MAX_DRAFT_FILE_BYTES)
                elif not (
                    isinstance(ref.get("imports"), list)
                    and ref["imports"]
                    and all(isinstance(name, str) and name.strip() for name in ref["imports"])
                ):
                    raise ValueError("Draft library mapping requires explicit imports")
            except (OSError, ValueError, TypeError):
                usable = False
                break
        if not usable:
            issues.append("Draft unit maps a missing or malformed declaration source")
            continue
        mapped.update(linked)
        dispositions = unit.get("clause_dispositions", [])
        if not isinstance(dispositions, list):
            issues.append("Draft clause dispositions must be a list")
            continue
        seen_clauses: set[str] = set()
        for row in dispositions:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("obligation_id"), str)
                or row["obligation_id"] not in clauses
                or clauses[row["obligation_id"]]["source_id"] not in linked
                or row["obligation_id"] in seen_clauses
                or not isinstance(row.get("disposition"), str)
                or row.get("disposition") not in _CLAUSE_DISPOSITIONS
                or not isinstance(row.get("rationale"), str)
                or not row["rationale"].strip()
                or not isinstance(row.get("declaration_names"), list)
                or not all(
                    isinstance(name, str) and name in names for name in row["declaration_names"]
                )
                or (
                    row["disposition"] in {"represented", "pending_proof"}
                    and not row["declaration_names"]
                )
            ):
                issues.append("Malformed, duplicate or unknown draft clause disposition")
                continue
            key = row["obligation_id"]
            seen_clauses.add(key)
            accounted_clauses.add(key)
            if row["disposition"] not in {"represented", "pending_proof"}:
                unresolved.add(key)
    missing = required - mapped
    missing_regions = regions - accounted_regions
    missing_clauses = set(clauses) - accounted_clauses
    return {
        "complete": not (issues or missing or missing_regions or missing_clauses),
        "accepted": False,
        "publication_authority": False,
        "proof_authority": False,
        "source_ids": sorted(source_ids),
        "required_source_ids": sorted(required),
        "excluded_source_ids": sorted(excluded),
        "excluded_items": list(excluded.values()),
        "mapped_source_ids": sorted(mapped),
        "missing_source_ids": sorted(missing),
        "required_region_ids": sorted(regions),
        "accounted_region_ids": sorted(accounted_regions),
        "missing_region_ids": sorted(missing_regions),
        "required_clause_ids": sorted(clauses),
        "clause_ids": sorted(all_clauses),
        "excluded_clause_ids": sorted(excluded_clauses),
        "accounted_clause_ids": sorted(accounted_clauses),
        "missing_clause_ids": sorted(missing_clauses),
        "unresolved_clause_ids": sorted(unresolved),
        "counts": {
            "source_items": len(source_ids),
            "required_source_items": len(required),
            "excluded_source_items": len(excluded),
            "mapped_source_items": len(mapped),
            "regions": len(regions),
            "accounted_regions": len(accounted_regions),
            "known_clauses": len(all_clauses),
            "required_known_clauses": len(clauses),
            "excluded_known_clauses": len(excluded_clauses),
            "accounted_known_clauses": len(accounted_clauses),
            "unresolved_known_clauses": len(unresolved),
        },
        "issues": issues,
        "proposal_sha256": proposal_hash,
        "raw_proposal": proposal,
        "raw_proposed_corrections": proposal.get("corrections", []),
    }
