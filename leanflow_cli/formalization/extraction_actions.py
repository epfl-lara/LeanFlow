"""Retry source extraction from frozen blobs while retaining prior repaired revisions."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained
from leanflow_cli.formalization.records import source_bytes
from leanflow_cli.formalization.source_inventory import inventory_source

RETRYABLE_PDF_ERRORS = {
    "pdf_metadata_failed",
    "pdf_extraction_failed",
    "pdf_page_count_mismatch",
    "pdf_extractor_missing",
    "pdf_pages_unknown",
    "pdf_page_census_unverified",
}


def has_user_extraction_work(record: dict[str, Any]) -> bool:
    """Protect existing repairs and downstream authority from automatic inventory refresh."""
    return (
        bool(
            record.get("actions")
            or record.get("units")
            or record.get("checks")
            or record.get("reviews")
            or record.get("handoff")
        )
        or record.get("status") != "inventory_pending"
        or bool(record["inventory"].get("audit_complete"))
        or any(page.get("transcription") for page in record["inventory"].get("pages", []))
        or any(region.get("actor") == "user" for region in record["inventory"]["regions"])
    )


def should_refresh_raw_extraction(record: dict[str, Any], inventory: dict[str, Any]) -> bool:
    """Retry transient failures automatically only when no user choices can be discarded."""
    return (
        not has_user_extraction_work(record)
        and any(
            row.get("blocking") and row["code"] in RETRYABLE_PDF_ERRORS
            for row in record["inventory"]["diagnostics"]
        )
        and inventory != record["inventory"]
    )


def retry_extraction(record: dict[str, Any], reason: str) -> None:
    """Rebuild frozen source candidates and revoke mappings, consent and coverage choices."""
    inventory = record["inventory"]
    with tempfile.TemporaryDirectory(prefix="leanflow-frozen-extraction-") as directory:
        root = Path(directory)
        for relative in record["source_files"]:
            target = contained(root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source_bytes(record, relative))
        companion = inventory.get("companion_pdf", {}).get("file")
        replacement = inventory_source(
            root,
            contained(root, inventory["source_relative"]),
            companion_pdf=contained(root, companion) if companion else None,
        )
    if {
        name: data["sha256"].removeprefix("sha256:") for name, data in replacement["files"].items()
    } != record["source_files"]:
        raise ValueError("Frozen re-extraction changed the selected source bundle")
    record.update(inventory=replacement, units=[], setup_notes="")
    record.pop("slice_id", None)
    record["extraction_retry"] = {
        "parent_revision_id": record.get("parent_revision_id"),
        "reason": reason,
    }
