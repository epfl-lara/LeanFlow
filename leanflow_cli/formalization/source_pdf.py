"""Extract PDF page text and navigable bounds while retaining mandatory coverage repair."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .source_provenance import digest, identity


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Bound external extraction time without truncating successful source output."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _page(element: ET.Element, index: int) -> dict[str, Any]:
    """Preserve physical page identity and word coordinates from Poppler XHTML."""
    parts: list[str] = []
    boxes: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    length = 0
    for block in element.findall(".//{*}block"):
        block_start = length
        for line in block.findall(".//{*}line"):
            for number, word in enumerate(line.findall("{*}word")):
                text = "".join(word.itertext())
                if number:
                    parts.append(" ")
                    length += 1
                start = length
                parts.append(text)
                length += len(text)
                boxes.append(
                    {
                        "text_start": start,
                        "text_end": length,
                        "text": text,
                        "x_min": float(word.attrib["xMin"]),
                        "y_min": float(word.attrib["yMin"]),
                        "x_max": float(word.attrib["xMax"]),
                        "y_max": float(word.attrib["yMax"]),
                    }
                )
            parts.append("\n")
            length += 1
        blocks.append({"text_start": block_start, "text_end": length})
        parts.append("\n")
        length += 1
    return {
        "index": index,
        "label": str(index + 1),
        "label_kind": "physical_page",
        "text": "".join(parts),
        "width": float(element.attrib["width"]),
        "height": float(element.attrib["height"]),
        "rotation": None,
        "boxes": boxes,
        "blocks": blocks,
        "mapping_method": "poppler_bbox",
        "mapping_precision": "approximate",
        "coordinate_origin": "top_left",
        "coordinate_units": "pdf_points",
    }


def _layout_pages(text: str) -> list[dict[str, Any]]:
    """Retain form-feed page boundaries without inventing unavailable word coordinates."""
    texts = text.split("\f")
    if texts and not texts[-1].strip():
        texts.pop()
    pages = []
    for index, content in enumerate(texts):
        blocks = [
            {"text_start": match.start(), "text_end": match.end()}
            for match in re.finditer(r"\S[^\f]*?(?=\n\s*\n|\Z)", content)
        ]
        pages.append(
            {
                "index": index,
                "label": str(index + 1),
                "label_kind": "physical_page",
                "text": content,
                "boxes": [],
                "blocks": blocks,
                "mapping_method": "poppler_layout",
                "mapping_precision": "approximate",
            }
        )
    return pages


def inventory_pdf(source: Path, relative: str, document_id: str) -> dict[str, Any]:
    """Inventory frozen PDF bytes and require page-level audits even after text extraction."""
    raw = source.read_bytes()
    file_hash = digest(raw)
    diagnostics: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []

    def diagnostic(code: str, message: str, *, blocking: bool = True, **extra: Any) -> None:
        diagnostics.append(
            {"code": code, "message": message, "file": relative, "blocking": blocking, **extra}
        )

    count = 0
    with tempfile.TemporaryDirectory(prefix="leanflow-pdf-inventory-") as directory:
        frozen = Path(directory) / "source.pdf"
        frozen.write_bytes(raw)
        info_tool, text_tool = shutil.which("pdfinfo"), shutil.which("pdftotext")
        if info_tool:
            try:
                result = _run([info_tool, "-box", str(frozen)])
                page_count = re.search(r"^Pages:\s*(\d+)\s*$", result.stdout, re.MULTILINE)
                if result.returncode == 0 and page_count:
                    count = int(page_count[1])
                else:
                    diagnostic(
                        "pdf_metadata_failed",
                        "PDF metadata could not be read: " + result.stderr.strip(),
                    )
            except (OSError, subprocess.TimeoutExpired) as error:
                diagnostic("pdf_metadata_failed", str(error))
        if text_tool:
            extraction_error = ""
            try:
                result = _run([text_tool, "-bbox-layout", "-enc", "UTF-8", str(frozen), "-"])
                if result.returncode != 0:
                    extraction_error = result.stderr.strip()
                else:
                    tree = ET.fromstring(result.stdout)
                    pages = [
                        _page(element, index)
                        for index, element in enumerate(tree.findall(".//{*}page"))
                    ]
            except (
                OSError,
                subprocess.TimeoutExpired,
                ET.ParseError,
                ValueError,
                KeyError,
            ) as error:
                extraction_error = str(error)
            if not pages:
                try:
                    result = _run([text_tool, "-layout", "-enc", "UTF-8", str(frozen), "-"])
                    if result.returncode == 0:
                        pages = _layout_pages(result.stdout)
                        diagnostic(
                            "pdf_layout_fallback",
                            "Word-coordinate extraction failed; page text is available with approximate page-only navigation. "
                            + extraction_error,
                            blocking=False,
                        )
                    else:
                        extraction_error += "; page-text fallback: " + result.stderr.strip()
                except (OSError, subprocess.TimeoutExpired) as error:
                    extraction_error += "; page-text fallback: " + str(error)
                if not pages:
                    diagnostic(
                        "pdf_extraction_failed", "PDF text extraction failed: " + extraction_error
                    )
            if count and pages and len(pages) != count:
                diagnostic(
                    "pdf_page_count_mismatch",
                    f"Expected {count} pages; text extraction returned {len(pages)}",
                )
        else:
            diagnostic(
                "pdf_extractor_missing",
                "pdftotext is unavailable; install Poppler or provide a verified page transcription",
            )
    if not pages:
        pages = [
            {
                "index": index,
                "label": str(index + 1),
                "label_kind": "physical_page",
                "text": "",
                "boxes": [],
                "mapping_method": "unavailable",
                "mapping_precision": "unavailable",
            }
            for index in range(count)
        ]
    if not pages:
        diagnostic(
            "pdf_pages_unknown",
            "Physical page inventory could not be established; manual source repair is required",
        )
    items: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    for page in pages:
        page_index = page["index"]
        if not page["text"].strip():
            diagnostic(
                "pdf_text_missing",
                "Page has no extractable text; supply a verified transcription or repair the selected scope",
                page_index=page_index,
                repair_required=True,
            )
        for block_index, block in enumerate(page.get("blocks", [])):
            text = page["text"][block["text_start"] : block["text_end"]]
            match = re.match(
                r"\s*(Theorem|Lemma|Proposition|Corollary|Definition|Conjecture|Assumption|Notation|Claim|Remark)\b\s*([^\n.]*)",
                text,
                re.IGNORECASE,
            )
            if match is None:
                continue
            item_id = identity(
                "src",
                document_id + ":page:" + str(page_index) + ":block:" + str(block_index),
            )
            span = {
                "file": relative,
                "file_hash": file_hash,
                "byte_start": 0,
                "byte_end": len(raw),
                "coordinate_space": "pdf_page",
                "page_index": page_index,
                "text_start": block["text_start"],
                "text_end": block["text_end"],
                "mapping_precision": "approximate",
                "mapping_method": page["mapping_method"],
                "boxes": [
                    box
                    for box in page["boxes"]
                    if block["text_start"] <= box["text_start"] < block["text_end"]
                ],
            }
            items.append(
                {
                    "id": item_id,
                    "kind": match[1].lower(),
                    "claim_status": "unclassified",
                    "label": None,
                    "title": match[0].strip(),
                    "statement_spans": [span],
                    "proof_spans": [],
                    "context_refs": [],
                    "content_hash": digest(text),
                    "text": text,
                    "scope": "candidate",
                    "boundary_review_required": True,
                    "identity_basis": "physical_page_block_ordinal",
                }
            )
        regions.append(
            {
                "id": identity("region", document_id + ":page:" + str(page_index)),
                "file": relative,
                "byte_start": 0,
                "byte_end": len(raw),
                "file_hash": file_hash,
                "page_index": page_index,
                "coordinate_space": "pdf_page",
                "disposition": "unresolved",
                "item_ids": [
                    item["id"]
                    for item in items
                    if item["statement_spans"][0]["page_index"] == page_index
                ],
                "reason": "Page coverage and mathematical text fidelity require source audit",
                "repair_required": not bool(page["text"].strip()),
            }
        )
    if not pages:
        regions.append(
            {
                "id": identity("region", document_id + ":pdf-unreadable"),
                "file": relative,
                "byte_start": 0,
                "byte_end": len(raw),
                "file_hash": file_hash,
                "coordinate_space": "source_bytes",
                "disposition": "unresolved",
                "item_ids": [],
                "reason": "Unreadable PDF requires source repair",
                "repair_required": True,
            }
        )
    return {
        "files": {relative: {"sha256": file_hash, "encoding": "binary", "size": len(raw)}},
        "pages": pages,
        "items": items,
        "regions": regions,
        "diagnostics": diagnostics,
        "include_occurrences": [],
        "macro_definitions": [],
    }
