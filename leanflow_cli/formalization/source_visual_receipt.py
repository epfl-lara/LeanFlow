"""Require complete delivered companion-page evidence before portable acceptance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.source_visual import MAX_VISUAL_PAGES, FrozenImage
from leanflow_cli.formalization.source_visual_process import RENDER_FLAGS, RENDER_PROTOCOL


def validated_visual_evidence(
    record: dict[str, Any], review: dict[str, Any]
) -> list[dict[str, Any]]:
    """Authenticate every exact page image and require controller-recorded native delivery."""
    companion = record["inventory"].get("companion_pdf")
    rows = review.get("visual_evidence", [])
    if not companion:
        if rows:
            raise ValueError("Visual evidence cannot introduce an unselected PDF companion")
        return []
    pages = record["inventory"]["pages"]
    if not isinstance(rows, list) or not 1 <= len(rows) == len(pages) <= MAX_VISUAL_PAGES:
        raise ValueError("Accepted companion review requires every frozen physical page image")
    observed: dict[str, str] = {}
    for outcome in review.get("reviews", []):
        if outcome.get("accepted") is not True or outcome.get("cleanup_verified") is not True:
            raise ValueError("Visual source review requires independently closed positive reviews")
        for key, value in outcome.get("image_reads", {}).items():
            if key in observed and observed[key] != value:
                raise ValueError("Reviewed physical page image changed between source batches")
            observed[key] = value
    file = companion["file"]
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Visual source evidence entries must be structured records")
        renderer = row.get("renderer", {})
        binding = {
            "file": file,
            "pdf_sha256": record["source_files"][file],
            "physical_page_count": len(pages),
            "renderer": renderer,
            "flags": list(RENDER_FLAGS),
        }
        if (
            not isinstance(row, dict)
            or row.get("file") != file
            or row.get("page_index") != index
            or row.get("pdf_sha256") != binding["pdf_sha256"]
            or not isinstance(renderer, dict)
            or renderer.get("protocol") != RENDER_PROTOCOL
            or row.get("flags") != list(RENDER_FLAGS)
            or row.get("evidence_id")
            != "page-image-"
            + identity({"binding": binding, "index": index, "png_sha256": row.get("png_sha256")})
            or observed.get(row.get("evidence_id", "")) != row.get("png_sha256")
        ):
            raise ValueError("Companion acceptance lacks exact delivered source-page evidence")
        FrozenImage(row, Path(record["_directory"]) / "blobs").read()
    return rows
