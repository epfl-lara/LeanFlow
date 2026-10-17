"""Validate complete source coverage and explicit many-to-many translation units."""

from __future__ import annotations

import copy
from typing import Any

from leanflow_cli.formalization.clause_obligations import validate_dispositions
from leanflow_cli.formalization.corrections import apply_corrections
from leanflow_cli.formalization.evidence import identifier
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.lean_origin import imported_origin_owner
from leanflow_cli.formalization.lean_policy import POLICY_VERSION
from leanflow_cli.formalization.source_reconciliation import apply_reconciliation
from leanflow_cli.formalization.source_review_regions import expand_coverage


def apply_proposal(record: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """Bind model mappings to frozen source identities without granting exclusions."""
    if (
        proposal.get("schema_version") != 1
        or proposal.get("base_revision_id") != record["revision_id"]
    ):
        raise ValueError("Proposal must name the current frozen source revision")
    result = copy.deepcopy(record)
    if {"correction_authorities", "correction_evidence"}.intersection(proposal):
        raise ValueError("Author proposals cannot grant correction authority or checked evidence")
    result["inventory"] = copy.deepcopy(complete_inventory(record))
    from leanflow_cli.formalization.slice_gate import protocol_enabled, slice_regions

    partial = protocol_enabled(record) and proposal.get("phase") == "representative_slice"
    if protocol_enabled(record) and proposal.get("phase") not in {
        "representative_slice",
        "complete",
    }:
        raise ValueError("Preparation workflow proposals require an explicit controller phase")
    result["proposal_phase"] = proposal.get("phase", "complete")
    if protocol_enabled(record):
        result["full_inventory"] = copy.deepcopy(result["inventory"])
    try:
        apply_reconciliation(result, proposal.get("source_reconciliation"))
    except ValueError as error:
        raise ValueError(
            "Blocking extraction diagnostics require exact source reconciliation: " + str(error)
        ) from error
    apply_corrections(result, proposal.get("corrections", []))
    selected_source_ids = set(record["slice_source_ids"]) if partial else None
    if partial:
        selected_regions = slice_regions(record)
        result["inventory"]["regions"] = [
            region for region in result["inventory"]["regions"] if region["id"] in selected_regions
        ]
    items = {item["id"]: item for item in result["inventory"]["items"]}
    regions = {region["id"]: region for region in result["inventory"]["regions"]}
    coverage = proposal.get("coverage")
    if isinstance(coverage, list):
        try:
            coverage = expand_coverage(result["inventory"]["regions"], coverage)
        except ValueError as error:
            raise ValueError(
                "Coverage must account for every selected document region or exact review group: "
                + str(error)
            ) from error
    if not isinstance(coverage, list) or len(coverage) != len(regions):
        raise ValueError("Coverage must account for every selected document region")
    seen: set[str] = set()
    for row in coverage:
        if not isinstance(row, dict) or row.get("region_id") not in regions:
            raise ValueError("Coverage names an unknown region")
        key = row["region_id"]
        if key in seen:
            raise ValueError("Coverage contains a duplicate region")
        seen.add(key)
        region = regions[key]
        if region.get("repair_required"):
            raise ValueError(
                "Unextractable source regions require verified repair or an explicit user scope exclusion"
            )
        disposition = row.get("disposition")
        if disposition not in {"item", "supporting_context", "non_mathematical", "excluded"}:
            raise ValueError("Unresolved document regions must be repaired before review")
        if disposition == "excluded" and not (
            region.get("disposition") == "excluded" and region.get("actor") in {"user", "source"}
        ):
            raise ValueError("Only an explicit user action can exclude source material")
        linked = row.get("item_ids", [])
        if not isinstance(linked, list) or any(key not in items for key in linked):
            raise ValueError("Region coverage references an unknown source item")
        if disposition == "item" and not linked:
            raise ValueError("A represented region needs linked source items")
        explicit_item_exclusion = (
            disposition == "excluded"
            and region.get("disposition") == "excluded"
            and region.get("actor") == "user"
            and all(items[key].get("scope") == "excluded" for key in region.get("item_ids", []))
        )
        if region.get("item_ids") and disposition != "item" and not explicit_item_exclusion:
            raise ValueError("Detected mathematical material cannot be silently reclassified")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Every region disposition requires an auditable reason")
        region.update(
            disposition=disposition,
            item_ids=linked,
            reason=reason,
            actor=region.get("actor", "author"),
        )
    units = proposal.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError("A formalization needs explicit translation units")
    seen_units: set[str] = set()
    covered: set[str] = set()
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError("Invalid translation unit")
        key = identifier(str(unit.get("id", "")))
        if key in seen_units:
            raise ValueError("Translation unit IDs must be unique")
        seen_units.add(key)
        source_ids = unit.get("required_source_ids")
        if not isinstance(source_ids, list) or any(key not in items for key in source_ids):
            raise ValueError("Translation unit names unknown source items")
        if selected_source_ids is not None and set(source_ids) - selected_source_ids:
            raise ValueError("Representative slice units must use only its selected source IDs")
        if any(items[key].get("scope") == "excluded" for key in source_ids):
            raise ValueError("An excluded source item cannot count toward included coverage")
        note = unit.get("representation_note")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Every unit needs a representation or helper rationale")
        declarations = unit.get("declarations")
        if not isinstance(declarations, list) or not declarations:
            raise ValueError("Every unit must map to exact compiled declarations")
        for declaration in declarations:
            if (
                not isinstance(declaration, dict)
                or not isinstance(declaration.get("name"), str)
                or not declaration["name"].strip()
            ):
                raise ValueError("Lean references require exact fully qualified names")
            if not declaration.get("file") and not declaration.get("imports"):
                raise ValueError("Library reuse must specify its pinned import modules")
        validate_dispositions(result, unit)
        dependencies = unit.get("depends_on_units", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(key, str) for key in dependencies
        ):
            raise ValueError("Unit dependencies must be unit IDs")
        covered.update(source_ids)
    required = (
        selected_source_ids
        if selected_source_ids is not None
        else {key for key, item in items.items() if item.get("scope") != "excluded"}
    )
    if required - covered:
        raise ValueError("Missing source item mappings: " + ", ".join(sorted(required - covered)))
    for unit in units:
        if set(unit.get("depends_on_units", [])) - seen_units:
            raise ValueError("Translation dependency points outside this revision")
    ordered = []
    remaining = list(units)
    ready: set[str] = set()
    while remaining:
        available = [unit for unit in remaining if set(unit.get("depends_on_units", [])) <= ready]
        if not available:
            raise ValueError("Translation units contain a dependency cycle")
        ordered.extend(available)
        ready.update(unit["id"] for unit in available)
        remaining = [unit for unit in remaining if unit["id"] not in ready]
    if any(d.get("blocking") for d in result["inventory"].get("diagnostics", [])):
        raise ValueError("Blocking extraction diagnostics require explicit source repair")
    from leanflow_cli.formalization.source_pdf import assert_pdf_review_ready

    assert_pdf_review_ready(result["inventory"])
    result["units"] = copy.deepcopy(ordered)
    result["setup_notes"] = str(proposal.get("setup_notes", ""))
    result["inventory"]["audit_complete"] = not partial
    result["inventory"]["slice_audit_complete"] = partial
    if partial:
        result["setup_notes"] += (
            "\nController phase: representative_slice. Review only the selected translation and "
            "its supporting coverage. Complete original source files remain retrievable; "
            "unselected mathematics has no acceptance or publication authority."
        )
    return result


def bind_declarations(record: dict[str, Any], checks: dict[str, Any]) -> None:
    """Require every mapping and ordinary new declaration to have kernel evidence."""
    registry = {row["name"]: row for row in checks["declarations"]}
    if len(registry) != len(checks["declarations"]):
        raise ValueError("Duplicate compiled declaration identities cannot replace file bindings")
    mapped: set[str] = set()
    for unit in record["units"]:
        for reference in unit["declarations"]:
            row = registry.get(reference["name"])
            origin = (row or {}).get(
                "declaration_origin", "generated" if (row or {}).get("file") else "dependency"
            )
            declared_origin = reference.get("declaration_origin")
            if (
                row is None
                or (declared_origin is not None and declared_origin != origin)
                or (reference.get("file") and reference["file"] != row.get("file"))
                or (origin == "generated" and reference.get("file") != row.get("file"))
            ):
                raise ValueError(
                    f"Mapping does not identify a compiled declaration: {reference['name']}"
                )
            reference.update(
                type_hash=row["type_hash"],
                body_hash=row["body_hash"],
                type=row["type"],
                conditional=row.get("conditional", False),
                declaration_origin=origin,
            )
            if origin == "existing_project":
                reference.update(
                    file=row["file"],
                    module=row["module"],
                    source_sha256=row["source_sha256"],
                )
            if row.get("file") and type(row.get("line_start")) is int and row["line_start"] > 0:
                reference["line_start"] = row["line_start"]
            mapped.add(row["name"])

    def compiler_ancestors(name: str, active: frozenset[str] = frozenset()) -> set[str]:
        if name in active:
            raise ValueError("Compiler-generated declaration ownership has a cycle")
        direct = set(registry[name].get("generated_from", []))
        unknown = direct - set(registry)
        if unknown:
            # Simplification can inline an imported definition's lazy equation.
            # Its exact source-backed consumer owns this generated helper; the
            # borrowed parent remains provenance only, outside authored mappings.
            row = registry[name]
            parent = imported_origin_owner(row, checks.get("origin_parent_profiles", {}))
            origin = checks.get("origin_parent_profiles", {}).get(next(iter(unknown)), {})
            if (
                len(direct) != 1
                or checks.get("accepted") is not True
                or checks.get("policy_id") != POLICY_VERSION
                or not isinstance(parent, dict)
                or parent.get("declaration_origin") != "existing_project"
                or not parent.get("file")
                or not parent.get("source_sha256")
                or checks.get("admitted_project_sources", {}).get(parent["file"])
                != parent["source_sha256"]
                or any(
                    origin.get(key) != parent.get(key)
                    for key in ("file", "source_sha256", "declaration_origin")
                )
            ):
                raise ValueError("Compiler-generated declaration has an unknown owner")
            owners = {
                owner["name"]
                for owner in registry.values()
                if owner["name"] in mapped
                and not owner.get("generated_from")
                and isinstance(owner.get("source_range"), dict)
                and owner.get("module") == row.get("module")
                and owner.get("file") == row.get("file")
                and {name, parent["name"]} & set(owner.get("direct_dependencies", []))
            }
            if not owners:
                raise ValueError("Imported compiler equation has no exact mapped source consumer")
            return owners
        return direct | {
            ancestor
            for parent in direct
            for ancestor in compiler_ancestors(parent, active | {name})
        }

    for row in sorted(registry.values(), key=lambda value: value["name"]):
        parents = row.get("generated_from", [])
        if (
            row.get("declaration_origin", "generated" if row.get("file") else "dependency")
            != "generated"
            or not parents
            or not row.get("generation_kind")
            or row["name"] in mapped
        ):
            continue
        ancestors = compiler_ancestors(row["name"])
        for unit in record["units"]:
            unit_names = {reference["name"] for reference in unit["declarations"]}
            if ancestors & unit_names:
                reference = {
                    "name": row["name"],
                    "file": row["file"],
                    "type_hash": row["type_hash"],
                    "body_hash": row["body_hash"],
                    "type": row["type"],
                    "conditional": row.get("conditional", False),
                    "generated_from": parents,
                    "generation_kind": row["generation_kind"],
                    "compiler_owner_names": sorted(ancestors & unit_names),
                }
                if type(row.get("line_start")) is int and row["line_start"] > 0:
                    reference["line_start"] = row["line_start"]
                unit["declarations"].append(reference)
                mapped.add(row["name"])
    missing = {
        row["name"]
        for row in registry.values()
        if row.get("declaration_origin", "generated" if row.get("file") else "dependency")
        == "generated"
        and row["name"] not in mapped
    }
    if missing:
        raise ValueError("Unmapped compiled declarations: " + ", ".join(sorted(missing)))
    provenance: dict[str, set[str]] = {name: set() for name in registry}
    for unit in record["units"]:
        for reference in unit["declarations"]:
            provenance[reference["name"]].update(unit.get("required_source_ids", []))
    changed = True
    while changed:
        changed = False
        for name, row in registry.items():
            inherited = set().union(
                *(
                    provenance.get(dependency, set())
                    for dependency in row.get("direct_dependencies", [])
                )
            )
            if inherited - provenance[name]:
                provenance[name].update(inherited)
                changed = True
    for unit in record["units"]:
        required = set(unit.get("required_source_ids", []))
        for reference in unit["declarations"]:
            row = registry[reference["name"]]
            if not (row.get("pending") or row.get("depends_on")):
                continue
            if not provenance[row["name"]]:
                raise ValueError(f"Pending proof requires source provenance: {row['name']}")
            required.update(provenance[row["name"]])
        unit["derived_source_ids"] = sorted(required - set(unit.get("required_source_ids", [])))
        unit["required_source_ids"] = sorted(required)
        validate_dispositions(record, unit, compiled=True)
