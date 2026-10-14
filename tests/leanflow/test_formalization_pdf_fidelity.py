"""Keep mixed-content PDF mathematics blocked until verified transcription and boundary repair."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from leanflow_cli.formalization.actions import apply_action
from leanflow_cli.formalization.proposal_schema import apply_proposal
from leanflow_cli.formalization.records import (
    create_document,
    load_document,
    load_receipt,
    save_receipt,
    source_artifact_id,
    source_bytes,
)
from leanflow_cli.formalization.service import act_document
from leanflow_cli.formalization.source_pdf import assert_pdf_review_ready


def _mixed_pdf(path: Path) -> None:
    """Render the essential hypothesis x <= 0 as an image, outside the text layer."""
    glyphs = [
        ["00000", "10001", "01010", "00100", "01010", "10001", "00000"],
        ["00001", "00010", "00100", "01000", "00100", "00010", "00001"],
        ["00000", "00000", "11111", "00000", "11111", "00000", "00000"],
        ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    ]
    image = bytes(
        int(("0".join(glyph[row] for glyph in glyphs) + "0" * 9)[start : start + 8], 2)
        for row in range(7)
        for start in range(0, 32, 8)
    )
    content = b"BT /F1 12 Tf 72 720 Td (Theorem 1. For an integer satisfying the rasterized hypothesis, the bound holds.) Tj ET\nq 184 0 0 56 72 640 cm /Clause Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> /XObject << /Clause 6 0 R >> >> /Contents 5 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
        f"<< /Type /XObject /Subtype /Image /Width 32 /Height 7 /ColorSpace /DeviceGray /BitsPerComponent 1 /Decode [1 0] /Length {len(image)} >>\nstream\n".encode()
        + image
        + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(data)


def _proposal(record: dict) -> dict:
    return {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "units": record["units"],
        "coverage": [
            {
                "region_id": region["id"],
                "disposition": "item" if region.get("item_ids") else "supporting_context",
                "item_ids": region.get("item_ids", []),
                "reason": "All extracted source evidence reviewed; syntactic PASS",
            }
            for region in record["inventory"]["regions"]
        ],
    }


@pytest.mark.skipif(
    not shutil.which("pdftotext") or not shutil.which("pdfinfo"), reason="Poppler unavailable"
)
def test_mixed_text_raster_claim_requires_repair_then_reestablished_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed.pdf"
    _mixed_pdf(source)
    original = source.read_bytes()
    record = create_document(tmp_path, source)
    item_id = record["inventory"]["items"][0]["id"]
    assert "rasterized hypothesis" in record["inventory"]["pages"][0]["text"]
    assert "x <= 0" not in record["inventory"]["pages"][0]["text"]
    record["units"] = [
        {
            "id": "unit-bound",
            "required_source_ids": [item_id],
            "declarations": [{"imports": ["Init"], "name": "True"}],
            "representation_note": "A bound",
        }
    ]
    with pytest.raises(ValueError, match="verified repair"):
        apply_proposal(record, _proposal(record))
    with pytest.raises(ValueError, match="verified transcription"):
        apply_action(
            record,
            {
                "type": "classify_region",
                "region_id": record["inventory"]["regions"][0]["id"],
                "classification": "supporting_context",
                "reason": "Acknowledged extraction uncertainty",
            },
        )
    with pytest.raises(ValueError, match="full-page transcription"):
        assert_pdf_review_ready(record["inventory"])

    save_receipt(
        record,
        {
            "revision_id": record["revision_id"],
            "status": "accepted_draft",
            "reviews": [{"decision": "PASS"}],
        },
    )
    old_revision = record["revision_id"]
    repaired = act_document(
        tmp_path,
        record["document_id"],
        old_revision,
        {
            "type": "replace_page_transcription",
            "page_index": 0,
            "text": "Theorem 1. For an integer x <= 0, the bound holds.",
            "verified": True,
            "reason": "Checked the complete rendered page, including the raster hypothesis",
        },
    )
    changed = load_document(tmp_path, record["document_id"])
    assert not changed["inventory"]["items"][0]["statement_spans"]
    assert changed["inventory"]["items"][0]["boundary_review_required"]
    assert not changed["units"] and not changed["checks"] and not changed["reviews"]
    assert changed["handoff"] is None and load_receipt(changed) is None
    assert not repaired["can_prove"]
    assert changed["inventory"]["pages"][0]["transcription_history"][0]["text"]
    assert source_bytes(changed, "mixed.pdf") == original == source.read_bytes()
    assert load_receipt(load_document(tmp_path, record["document_id"], old_revision))["reviews"]
    assert any(row["blocking"] for row in changed["inventory"]["diagnostics"])
    with pytest.raises(ValueError, match="revision changed"):
        act_document(
            tmp_path, record["document_id"], old_revision, {"type": "replace_page_transcription"}
        )
    boundary = act_document(
        tmp_path,
        record["document_id"],
        changed["revision_id"],
        {
            "type": "update_boundary",
            "item_id": item_id,
            "source_refs": [
                {
                    "artifact_id": source_artifact_id(changed, "mixed.pdf"),
                    "page_index": 0,
                    "text": changed["inventory"]["pages"][0]["text"],
                }
            ],
        },
    )
    current = load_document(tmp_path, record["document_id"])
    current["units"] = record["units"]
    assert not any(row["blocking"] for row in current["inventory"]["diagnostics"])
    assert current["inventory"]["items"][0]["text"].endswith("bound holds.")
    assert not boundary["can_prove"]
    assert_pdf_review_ready(current["inventory"])
    assert apply_proposal(current, _proposal(current))["inventory"]["audit_complete"]


def test_legacy_extraction_or_forged_verification_cannot_supply_pdf_authority() -> None:
    inventory = {
        "source_kind": "pdf",
        "page_inventory": {"verified": True, "method": "pdfinfo", "physical_page_count": 1},
        "pages": [{"index": 0, "text": "Readable theorem"}],
        "regions": [
            {"coordinate_space": "pdf_page", "page_index": 0, "disposition": "supporting_context"}
        ],
    }
    with pytest.raises(ValueError, match="full-page transcription"):
        assert_pdf_review_ready(inventory)
    inventory["pages"][0].update(
        fidelity={"status": "verified_transcription"},
        transcription={"actor": "user", "verified": True, "content_hash": "old text hash"},
    )
    with pytest.raises(ValueError, match="full-page transcription"):
        assert_pdf_review_ready(inventory)
    inventory["regions"][0].update(disposition="excluded", actor="user")
    assert_pdf_review_ready(inventory)


def test_transcription_reopens_split_regions_and_revokes_shared_mapping_and_consent() -> None:
    record = {
        "parent_revision_id": "old",
        "units": [{"id": "u", "required_source_ids": ["a", "b"]}],
        "actions": [],
        "inventory": {
            "source_kind": "pdf",
            "source_relative": "paper.pdf",
            "pages": [{"index": 0, "text": "Old page", "boxes": []}],
            "items": [
                {
                    "id": "a",
                    "text": "Old claim",
                    "proof_authorization": {"authorized": True},
                    "statement_spans": [{"coordinate_space": "pdf_page", "page_index": 0}],
                },
                {"id": "b", "statement_spans": [], "text": "Other claim"},
            ],
            "regions": [
                {
                    "id": key,
                    "coordinate_space": "pdf_page",
                    "page_index": 0,
                    "item_ids": ["a"],
                    "disposition": "item",
                }
                for key in ("left", "right")
            ],
            "diagnostics": [],
        },
    }
    apply_action(
        record,
        {
            "type": "replace_page_transcription",
            "page_index": 0,
            "text": "Corrected page",
            "reason": "Every formula verified",
            "verified": True,
        },
    )
    assert len(record["inventory"]["regions"]) == 1
    assert record["inventory"]["regions"][0]["disposition"] == "unresolved"
    assert not record["units"]
    assert "proof_authorization" not in record["inventory"]["items"][0]
    assert (
        record["inventory"]["pages"][0]["transcription_history"][0]["affected_items"][0]["text"]
        == "Old claim"
    )
