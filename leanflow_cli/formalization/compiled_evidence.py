"""Combine compiled evidence without confusing inspection scope with declaration identity."""

from __future__ import annotations

import copy
from typing import Any

from leanflow_cli.formalization.evidence import digest


def assert_reuse_sources_unchanged(
    declarations: list[dict[str, Any]], baseline: dict[str, bytes], changed: dict[str, bytes]
) -> None:
    """Refuse reuse evidence elaborated from controller-modified existing project sources."""
    for row in declarations:
        if row.get("declaration_origin") != "existing_project":
            continue
        relative = row.get("file")
        if (
            relative not in baseline
            or row.get("source_sha256") != digest(baseline[relative])
            or (relative in changed and changed[relative] != baseline[relative])
        ):
            raise ValueError(
                "Existing project reuse requires unchanged original source bytes; "
                f"move reusable declarations to an unchanged module: {row['name']}"
            )


def merge_library_evidence(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Retain the richest evidence and reject changes to actual compiled meaning."""
    contextual = {
        "semantic_surface",
        "body",
        "body_hash",
        "body_omitted",
        "pretty_body",
        "pretty_type",
        "direct_dependencies",
    }
    rows: dict[str, dict[str, Any]] = {}
    for source in [*existing, *incoming]:
        row = copy.deepcopy(source)
        name = row["name"]
        previous = rows.get(name)
        if previous is None:
            rows[name] = row
            continue
        old_identity = {key: value for key, value in previous.items() if key not in contextual}
        new_identity = {key: value for key, value in row.items() if key not in contextual}
        if old_identity != new_identity:
            raise ValueError(f"Compiled library meaning differs between inspections: {name}")
        old_omitted = previous.get("body_omitted", False)
        new_omitted = row.get("body_omitted", False)
        if (
            not old_omitted
            and not new_omitted
            and any(previous.get(key) != row.get(key) for key in ("body", "body_hash"))
        ):
            raise ValueError(f"Compiled library body differs between inspections: {name}")
        merged = row if old_omitted and not new_omitted else previous
        merged["semantic_surface"] = previous.get("semantic_surface", True) or row.get(
            "semantic_surface", True
        )
        merged["direct_dependencies"] = sorted(
            set(previous.get("direct_dependencies", [])) | set(row.get("direct_dependencies", []))
        )
        rows[name] = merged
    return [rows[name] for name in sorted(rows)]


def merge_reused_declarations(checks: dict[str, Any], result: dict[str, Any]) -> None:
    """Merge read-only project/library roots without granting generated ownership."""
    rows = {row["name"]: row for row in checks["declarations"]}
    for row in result["declarations"]:
        name = row["name"]
        previous = rows.get(name)
        origin = row.get("declaration_origin", "generated" if row.get("file") else "dependency")
        previous_origin = (
            previous.get(
                "declaration_origin", "generated" if previous.get("file") else "dependency"
            )
            if previous is not None
            else None
        )
        if origin not in {"existing_project", "dependency"} or previous_origin == "generated":
            raise ValueError(f"A project declaration cannot be mapped as library reuse: {name}")
        if origin == "existing_project" and (
            not row.get("file") or not row.get("source_sha256") or row.get("pending")
        ):
            raise ValueError(
                f"Existing project reuse requires immutable verified source evidence: {name}"
            )
        if origin == "dependency" and row.get("file"):
            raise ValueError(f"Dependency reuse cannot claim project source ownership: {name}")
        if previous is not None and previous != row:
            raise ValueError(f"Reused declaration differs between inspections: {name}")
        rows[name] = row
    checks["declarations"] = list(rows.values())
    checks["profiles"].update(result["profiles"])
    checks["library_declarations"] = merge_library_evidence(
        checks.get("library_declarations", []), result.get("library_declarations", [])
    )


def assert_rebuild_artifacts(initial: Any, rebuilt: Any) -> None:
    """Reject a whole-project rebuild that replaces any previously inspected artifact."""
    if not isinstance(initial, dict) or not initial or not isinstance(rebuilt, dict):
        raise ValueError("Independent compilation is missing its exact artifact evidence")
    for module, artifact in initial.items():
        current = rebuilt.get(module)
        if not isinstance(artifact, dict) or not artifact.get("sha256") or artifact != current:
            raise ValueError(f"Whole-project build changed an inspected artifact: {module}")
