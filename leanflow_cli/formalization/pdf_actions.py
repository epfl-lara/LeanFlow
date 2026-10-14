"""Replace verified PDF page evidence while retaining history and reopening affected boundaries."""

from __future__ import annotations

import copy
from typing import Any

from leanflow_cli.formalization.evidence import digest, identity
from leanflow_cli.formalization.source_pdf import (
    has_complete_page_census,
    has_verified_page_evidence,
)


def reconcile_extraction_diagnostics(inventory: dict[str, Any]) -> None:
    """Resolve text extraction failures only after every trusted physical page is audited."""
    if not has_complete_page_census(inventory) or not all(
        has_verified_page_evidence(inventory, page) for page in inventory.get("pages", [])
    ):
        return
    for diagnostic in inventory["diagnostics"]:
        if diagnostic["code"] in {
            "pdf_extractor_missing",
            "pdf_extraction_failed",
            "pdf_page_count_mismatch",
        }:
            diagnostic.update(
                blocking=False,
                resolution="verified_transcription",
                resolution_reason="Every physical page in the trusted PDF census has verified full-page evidence or an explicit complete user exclusion",
            )


def replace_transcription(
    record: dict[str, Any], page: dict[str, Any], text: str, reason: str
) -> None:
    """Replace one validated full-page transcription without carrying old claim authority."""
    inventory = record["inventory"]
    page_regions = [
        region
        for region in inventory["regions"]
        if region.get("coordinate_space") == "pdf_page"
        and region.get("page_index") == page["index"]
    ]
    if not page_regions:
        raise ValueError("The selected page has no frozen coverage region")
    file = page.get("file", inventory["source_relative"])
    affected = [
        item
        for item in inventory["items"]
        if any(
            span.get("coordinate_space") == "pdf_page" and span.get("page_index") == page["index"]
            for span in [*item["statement_spans"], *item.get("proof_spans", [])]
        )
    ]
    page.setdefault("transcription_history", []).append(
        {
            "revision_id": record.get("parent_revision_id"),
            "text": page["text"],
            "content_hash": digest(page["text"]),
            "mapping_method": page.get("mapping_method"),
            "transcription": copy.deepcopy(page.get("transcription")),
            "affected_items": copy.deepcopy(affected),
        }
    )
    affected_ids = {item["id"] for item in affected}
    removed_units = {
        unit["id"]
        for unit in record["units"]
        if affected_ids.intersection(unit["required_source_ids"])
    }
    record["units"] = [unit for unit in record["units"] if unit["id"] not in removed_units]
    for unit in record["units"]:
        unit["depends_on_units"] = [
            key for key in unit.get("depends_on_units", []) if key not in removed_units
        ]
    for item in affected:
        item.pop("proof_authorization", None)
        item.update(
            statement_spans=[],
            proof_spans=[],
            text="",
            content_hash="sha256:" + identity({"item_id": item["id"], "page_text": text}),
            boundary_review_required=True,
        )
        inventory["diagnostics"].append(
            {
                "id": "diagnostic-"
                + identity({"item_id": item["id"], "transcription": digest(text)})[:24],
                "code": "pdf_item_boundary_missing",
                "file": inventory["source_relative"],
                "page_index": page["index"],
                "item_id": item["id"],
                "blocking": True,
                "message": "Page transcription changed; re-establish this source item's complete statement boundary before review",
            }
        )
    page.update(
        text=text,
        boxes=[],
        blocks=[],
        mapping_method="manual_transcription",
        mapping_precision="approximate",
        transcription={
            "actor": "user",
            "verified": True,
            "reason": reason,
            "content_hash": digest(text),
        },
        fidelity={
            "status": "verified_transcription",
            "review_authority": "user_verified_full_page_transcription",
            "source_revision_id": record.get("parent_revision_id"),
            "content_hash": digest(text),
            "pdf_sha256": record.get("source_files", {}).get(file),
        },
    )
    replacement = {
        **page_regions[0],
        "id": "region-" + identity({"page": page["index"], "transcription": digest(text)})[:24],
        "text_start": 0,
        "text_end": len(text),
        "disposition": "unresolved",
        "repair_required": False,
        "item_ids": [],
        "reason": "Verified full-page transcription still requires a new coverage audit",
        "actor": "user",
    }
    inventory["regions"] = [
        region for region in inventory["regions"] if region not in page_regions
    ] + [replacement]
    for region in inventory["regions"]:
        if affected_ids.intersection(region.get("item_ids", [])):
            region.update(
                item_ids=[key for key in region["item_ids"] if key not in affected_ids],
                disposition="unresolved",
                reason="A linked source boundary changed with its PDF transcription",
            )
    for diagnostic in inventory["diagnostics"]:
        if diagnostic.get("page_index") == page["index"] and diagnostic["code"] in {
            "pdf_text_missing",
            "pdf_fidelity_unverified",
        }:
            diagnostic.update(
                blocking=False, resolution="verified_transcription", resolution_reason=reason
            )
    reconcile_extraction_diagnostics(inventory)
    inventory["extraction_status"] = (
        "blocked"
        if any(row.get("blocking") for row in inventory["diagnostics"])
        else "audit_required"
    )
    from leanflow_cli.formalization.source_companion import refresh_companion_refs

    refresh_companion_refs(inventory)
