"""Repair complete many-to-many translation units without granting declaration ownership."""

from __future__ import annotations

import re
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import identifier


def _validate_dependencies(units: list[dict[str, Any]]) -> None:
    """Reject duplicate unit IDs and redirected cycles before changing the working revision."""
    indexed = {unit["id"]: unit for unit in units}
    if len(indexed) != len(units):
        raise ValueError("Mapping repair would duplicate a retained review unit ID")
    dependencies = {key: set(unit.get("depends_on_units", [])) for key, unit in indexed.items()}
    if any(values - indexed.keys() for values in dependencies.values()):
        raise ValueError("Mapping dependencies must identify retained units in this revision")
    dependents: dict[str, set[str]] = {key: set() for key in indexed}
    for key, values in dependencies.items():
        for value in values:
            dependents[value].add(key)
    ready = deque(key for key, values in dependencies.items() if not values)
    visited = set()
    while ready:
        key = ready.popleft()
        visited.add(key)
        for child in dependents[key]:
            dependencies[child].remove(key)
            if not dependencies[child]:
                ready.append(child)
    if len(visited) != len(indexed):
        raise ValueError("Mapping repair would create a review dependency cycle")


def _references(values: Any) -> list[dict[str, Any]]:
    """Normalize exact local or imported declaration references, discarding stale evidence."""
    if not isinstance(values, list) or not values:
        raise ValueError("Mapping requires exact Lean references")
    normalized = []
    for ref in values:
        if (
            not isinstance(ref, dict)
            or not isinstance(ref.get("name"), str)
            or not ref["name"].strip()
        ):
            raise ValueError("Mapping references require fully qualified Lean names")
        result: dict[str, Any] = {"name": ref["name"].strip()}
        file = ref.get("file")
        if file:
            if (
                not isinstance(file, str)
                or Path(file).is_absolute()
                or ".." in Path(file).parts
                or not file.endswith(".lean")
            ):
                raise ValueError("Lean references require project-relative Lean file paths")
            result["file"] = file
        imports = ref.get("imports")
        if imports is not None:
            if (
                not isinstance(imports, list)
                or not imports
                or any(
                    not isinstance(module, str)
                    or not re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*", module
                    )
                    for module in imports
                )
            ):
                raise ValueError("Imported mappings require explicit Lean module names")
            result["imports"] = list(dict.fromkeys(imports))
        if not file and not imports:
            raise ValueError("Library reuse requires its pinned import modules")
        origin = ref.get("declaration_origin")
        if origin is not None:
            if origin not in {"generated", "existing_project", "dependency"}:
                raise ValueError("Unknown declaration origin")
            if origin == "dependency" and file:
                raise ValueError("Dependency reuse does not identify a project file")
            if origin == "generated" and not file:
                raise ValueError("Generated declarations require a project-relative Lean file")
            result["declaration_origin"] = origin
        normalized.append(result)
    return normalized


def set_mapping(record: dict[str, Any], action: dict[str, Any]) -> None:
    """Replace an explicitly selected whole unit or all complete intersecting units."""
    rationale = action.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("Mapping requires Lean references and representation rationale")
    references = _references(action.get("lean_refs"))
    source_ids = action.get("source_ids", [action.get("item_id")])
    items = {item["id"]: item for item in record["inventory"]["items"]}
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or any(not isinstance(key, str) or key not in items for key in source_ids)
        or len(set(source_ids)) != len(source_ids)
    ):
        raise ValueError("Mapping requires distinct source items from the selected revision")
    selected = set(source_ids)
    requested_id = action.get("unit_id")
    if requested_id is not None and not isinstance(requested_id, str):
        raise ValueError("Mapping review unit ID must be a string")
    key = identifier(requested_id) if isinstance(requested_id, str) else "unit-" + source_ids[0]
    current = next((unit for unit in record["units"] if unit["id"] == key), None)
    replaced = (
        [current]
        if current is not None and requested_id is not None
        else [
            unit for unit in record["units"] if selected.intersection(unit["required_source_ids"])
        ]
    )
    if any(set(unit["required_source_ids"]) - selected for unit in replaced):
        raise ValueError("Shared review units require all their source items in source_ids")
    removed_ids = {unit["id"] for unit in replaced}
    dependencies = action.get(
        "depends_on_units",
        sorted(
            {
                value
                for unit in replaced
                for value in unit.get("depends_on_units", [])
                if value not in removed_ids
            }
        ),
    )
    remaining = deepcopy([unit for unit in record["units"] if unit["id"] not in removed_ids])
    if (
        not isinstance(dependencies, list)
        or any(not isinstance(value, str) for value in dependencies)
        or set(dependencies) - {unit["id"] for unit in remaining}
        or key in dependencies
    ):
        raise ValueError("Mapping dependencies must identify retained units in this revision")
    for unit in remaining:
        unit["depends_on_units"] = list(
            dict.fromkeys(
                key if value in removed_ids else value for value in unit.get("depends_on_units", [])
            )
        )
    replacement = [
        *remaining,
        {
            "id": key,
            "required_source_ids": sorted(source_ids),
            "declarations": references,
            "representation_note": rationale.strip(),
            "depends_on_units": list(dict.fromkeys(dependencies)),
        },
    ]
    _validate_dependencies(replacement)
    record["units"] = replacement
