"""Inventory complete original document regions separately from bounded model context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .source_commands import command_references
from .source_pdf import inventory_pdf
from .source_provenance import digest, identity
from .source_structure import extract_items, inventory_regions
from .source_tex import TexBundle

EXTRACTION_VERSION = "provenance-scanner-v12"


def inventory_source(
    root: Path, source: Path, *, companion_pdf: Path | None = None
) -> dict[str, Any]:
    """Read a selected TeX root or PDF without modifying source or claiming completeness.

    Source spans are half-open original byte offsets. PDF page coordinates are
    explicitly tagged and refer to a page in the complete original PDF container.
    Callers must freeze and hash-check every returned original file before review.
    """
    root = root.resolve()
    selected = root / source if not source.is_absolute() else source
    cursor = selected
    while cursor != root and cursor != cursor.parent:
        if cursor.is_symlink():
            raise ValueError("Formalization source cannot traverse a symlink")
        cursor = cursor.parent
    source = selected.resolve()
    if not source.is_relative_to(root):
        raise ValueError("Formalization source must be inside the selected source root")
    if not source.is_file():
        raise ValueError(
            "Choose an existing TeX entrypoint or PDF; directory selection must resolve a root first"
        )
    relative = source.relative_to(root).as_posix()
    document_id = identity("doc", relative)
    if source.suffix.lower() == ".pdf":
        result = inventory_pdf(source, relative, document_id)
        kind = "pdf"
    elif source.suffix.lower() == ".tex":
        bundle = TexBundle(root, source, document_id)
        bundle.expand(source, relative)
        expanded = bundle.expanded()
        items = extract_items(bundle, expanded)
        commands = command_references(bundle, expanded)
        result = {
            "files": bundle.files,
            "items": items,
            "regions": inventory_regions(bundle, items),
            "diagnostics": bundle.diagnostics,
            "pages": [],
            "include_occurrences": bundle.occurrences,
            "macro_definitions": [
                definition for scan in bundle.scans.values() for definition in scan.definitions
            ],
            "command_references": commands,
        }
        kind = "latex"
    else:
        raise ValueError("Formalization inventory supports TeX entrypoints and PDFs")
    result.update(source_kind=kind, source_relative=relative)
    if companion_pdf is not None:
        from .source_companion import attach_companion

        attach_companion(result, inventory_source(root, companion_pdf))
    for index, diagnostic in enumerate(result["diagnostics"]):
        diagnostic["id"] = identity(
            "diagnostic", document_id + ":" + str(index) + ":" + diagnostic["code"]
        )
    result.update(
        {
            "schema_version": 1,
            "document_id": document_id,
            "source_kind": kind,
            "source_relative": relative,
            "extraction_version": EXTRACTION_VERSION,
            "structural_adapter": "provenance_scanner",
            "audit_complete": False,
            "extraction_status": (
                "blocked" if any(d["blocking"] for d in result["diagnostics"]) else "audit_required"
            ),
        }
    )
    result["bundle_hash"] = digest(
        json.dumps(
            {file: record["sha256"] for file, record in sorted(result["files"].items())},
            sort_keys=True,
        )
    )
    result["inventory_hash"] = digest(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return result
