"""Require an independently reviewed representative slice before complete translation and publication."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.corrections import assert_corrections_accepted
from leanflow_cli.formalization.evidence import contained, durable_json, identity, read_blob
from leanflow_cli.formalization.inventory_authority import (
    assert_complete_coverage,
    complete_inventory,
)
from leanflow_cli.formalization.records import (
    assert_source_current,
    current_selection_token,
    document_directory,
    load_document,
)
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.workflows.prover.source import lean_code_mask


def protocol_enabled(record: dict[str, Any]) -> bool:
    """Recognize controller-stamped native preparation and slice requirements."""
    return record.get("workflow_protocol") == 2


def slice_regions(record: dict[str, Any]) -> set[str]:
    """Select whole inventory regions touching the controller's representative source IDs."""
    selected = set(record["slice_source_ids"])
    inventory = complete_inventory(record)
    items = {item["id"]: item for item in inventory["items"]}
    spans = [
        span
        for key in selected
        for role in ("statement", "proof", "context")
        for span in items.get(key, {}).get(role + "_spans", [])
    ]
    result = set()
    for region in inventory["regions"]:
        if selected & set(region.get("item_ids", [])):
            result.add(region["id"])
            continue
        region_span = region.get("span", region)
        for span in spans:
            if span.get("file") != region_span.get("file"):
                continue
            if span.get("coordinate_space") == "pdf_page":
                intersects = span.get("page_index") == region_span.get("page_index")
            else:
                intersects = span.get("byte_start", 0) < region_span.get(
                    "byte_end", 0
                ) and region_span.get("byte_start", 0) < span.get("byte_end", 0)
            if intersects:
                result.add(region["id"])
    return result


def _binding(record: dict[str, Any]) -> dict[str, Any]:
    """Keep source and setup intent stable across automatic candidate revisions."""
    return {
        "document_id": record["document_id"],
        "source_bundle": record["source_bundle"],
        "source_files": record["source_files"],
        "preparation_id": record["preparation_id"],
        "preparation_plan_id": record["preparation_plan_id"],
        "slice_source_ids": sorted(record["slice_source_ids"]),
        "correction_authorities": identity(record.get("correction_authorities", [])),
        "source_inventory_identity": identity(
            {
                key: value
                for key, value in complete_inventory(record).items()
                if key not in {"audit_complete", "slice_audit_complete"}
            }
        ),
    }


def _load_slice(root: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    """Authenticate the selected immutable slice receipt without inferring approval from prose."""
    directory = document_directory(root, record["document_id"])
    pointer = contained(directory, "slice-current.json")
    if not pointer.is_file():
        return None
    selected = json.loads(pointer.read_text())
    if selected.get("status") != "reviewed":
        return None
    key = selected["slice_id"]
    receipt = json.loads(contained(directory, "slice-receipts/" + key + ".json").read_text())
    if receipt.get("slice_id") != "slice-" + identity(
        {name: value for name, value in receipt.items() if name != "slice_id"}
    ):
        raise ValueError("Representative slice receipt bytes changed")
    if receipt["binding"] != _binding(record):
        return None
    # Admission reads the original source base, before an author has proposed
    # corrections. A submitted slice/complete candidate must additionally retain
    # the exact reviewed corrections affecting the representative source items.
    if record.get("proposal_phase") in {"representative_slice", "complete"} and (
        receipt.get("corrections", []) != _slice_corrections(record)
    ):
        return None
    return receipt


def _slice_corrections(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain correction intent for selected units without forbidding disjoint later expansion."""
    selected = set(record["slice_source_ids"])
    return [row for row in record.get("corrections", []) if row["source_item_id"] in selected]


def slice_status(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Expose slice progress separately from full translation and proof authorization."""
    if not protocol_enabled(record):
        return {"phase": "complete", "required": False, "slice_id": None}
    receipt = _load_slice(root, record)
    return {
        "phase": "complete" if receipt else "representative_slice",
        "required": True,
        "slice_id": receipt["slice_id"] if receipt else None,
        "slice_source_ids": record["slice_source_ids"],
    }


def require_stage(root: Path, record: dict[str, Any], phase: Any) -> None:
    """Require current setup and the controller's phase before interpreting an author proposal."""
    if not protocol_enabled(record):
        return
    from leanflow_cli.formalization.preparation import assert_preparation_current

    try:
        assert_preparation_current(
            root,
            expected_id=record["preparation_id"],
            expected_environment=record.get("environment_id"),
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise RepreparationRequired(str(error)) from error
    expected = slice_status(root, record)["phase"]
    if phase != expected:
        raise ValueError(
            f"Formalization requires phase {expected}; a complete proposal cannot bypass the representative slice"
        )


def _meaning(row: dict[str, Any]) -> dict[str, Any]:
    """Freeze compiled declaration identities without ephemeral artifact paths."""
    return {
        key: row.get(key)
        for key in (
            "name",
            "module",
            "file",
            "declaration_origin",
            "kind",
            "type_hash",
            "body_hash",
            "conditional",
            "depends_on",
        )
    }


def save_reviewed_slice(
    root: Path, checked: dict[str, Any], review: dict[str, Any]
) -> dict[str, Any]:
    """Persist slice approval only after independent compiled and source-review gates pass."""
    record = checked["record"]
    if (
        not protocol_enabled(record)
        or record.get("proposal_phase") != "representative_slice"
        or checked.get("accepted") is not True
        or review.get("accepted") is not True
    ):
        raise ValueError(
            "Representative slice requires its exact compiled and source review acceptance"
        )
    require_stage(root, record, "representative_slice")
    from leanflow_cli.formalization.source_reconciliation import assert_reconciliation_reviewed

    assert_reconciliation_reviewed(record, review.get("reviews", []))
    assert_corrections_accepted(record, review.get("reviews", []))
    if (
        checked.get("selection_token") != current_selection_token(root, record["document_id"])
        or load_document(root, record["document_id"])["revision_id"] != record["revision_id"]
    ):
        raise ValueError("Selected source revision changed during representative-slice review")
    assert_source_current(root, record)
    layout = record["layout"]
    aggregator_hash = record["lean_files"].get(layout["aggregator"])
    aggregator_lines = (
        [
            line.strip()
            for line in lean_code_mask(
                read_blob(Path(record["_directory"]) / "blobs", aggregator_hash).decode("utf-8")
            ).splitlines()
            if line.strip()
        ]
        if aggregator_hash
        else []
    )
    import_only = bool(aggregator_lines) and all(
        re.fullmatch(r"import\s+[\w.'.]+(?:\s+[\w.'.]+)*", line) for line in aggregator_lines
    )
    files = {
        name: checksum
        for name, checksum in record["lean_files"].items()
        if (name != layout["aggregator"] or not import_only) and name != layout["root_file"]
    }
    receipt = {
        "schema_version": 1,
        "binding": _binding(record),
        "revision_id": record["revision_id"],
        "environment_id": record["environment_id"],
        "units": copy.deepcopy(record["units"]),
        "corrections": copy.deepcopy(_slice_corrections(record)),
        "files": files,
        "declarations": [_meaning(row) for row in record["checks"]["declarations"]],
        "review": copy.deepcopy(review),
        "review_sha256": identity(review),
        "status": "reviewed_representative_slice",
        "publication_authority": False,
    }
    receipt["slice_id"] = "slice-" + identity(receipt)
    directory = document_directory(root, record["document_id"])
    path = contained(directory, "slice-receipts/" + receipt["slice_id"] + ".json")
    if path.is_file() and json.loads(path.read_text()) != receipt:
        raise ValueError("Immutable representative slice receipt was replaced")
    durable_json(path, receipt)
    durable_json(
        contained(directory, "slice-current.json"),
        {"schema_version": 1, "slice_id": receipt["slice_id"], "status": "reviewed"},
    )
    return receipt


def assert_complete_slice(root: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    """Require full expansion to preserve the independently reviewed slice and its setup."""
    if not protocol_enabled(record):
        return None
    require_stage(root, record, "complete")
    if record.get("proposal_phase") != "complete":
        raise ValueError("A representative slice has no full-document publication authority")
    assert_complete_coverage(record)
    receipt = _load_slice(root, record)
    assert receipt is not None
    error = ""
    if receipt["environment_id"] != record["environment_id"]:
        error = "Representative slice compiler/dependency environment changed"
    elif any(
        record["lean_files"].get(name) != checksum for name, checksum in receipt["files"].items()
    ):
        error = "Complete translation changed a reviewed representative-slice file"
    else:
        rows = {row["name"]: _meaning(row) for row in record["checks"]["declarations"]}
        if any(rows.get(row["name"]) != row for row in receipt["declarations"]):
            error = "Complete translation changed a reviewed representative-slice declaration"
        units = {unit["id"]: unit for unit in record["units"]}
        for before in receipt["units"]:
            after = units.get(before["id"])
            if (
                after is None
                or after["representation_note"] != before["representation_note"]
                or after.get("clause_dispositions", []) != before.get("clause_dispositions", [])
                or set(before["required_source_ids"]) - set(after["required_source_ids"])
                or {row["name"] for row in before["declarations"]}
                - {row["name"] for row in after["declarations"]}
            ):
                error = "Complete translation changed a reviewed representative-slice mapping or representation"
                break
    if error:
        durable_json(
            contained(document_directory(root, record["document_id"]), "slice-current.json"),
            {
                "schema_version": 1,
                "slice_id": receipt["slice_id"],
                "status": "invalidated",
                "reason": error,
            },
        )
        raise ValueError(error + "; recheck and review phase representative_slice before expansion")
    return receipt
