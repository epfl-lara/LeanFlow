"""Freeze physical companion-page images without granting source or proof approval."""

from __future__ import annotations

import json
import re
import struct
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import contained, digest, durable_json, identity, put_blob
from leanflow_cli.formalization.publication_case_paths import assert_receipt_control_path
from leanflow_cli.formalization.records import source_bytes
from leanflow_cli.formalization.source_pdf import has_complete_page_census
from leanflow_cli.formalization.source_visual_process import (
    MAX_IMAGE_BYTES,
    RENDER_FLAGS,
    render_page,
    renderer_identity,
)

MAX_VISUAL_PAGES = 512


def _dimensions(raw: bytes) -> tuple[int, int]:
    """Reject oversized or non-PNG renderer output before native image delivery."""
    if (
        len(raw) > MAX_IMAGE_BYTES
        or len(raw) < 33
        or raw[:8] != b"\x89PNG\r\n\x1a\n"
        or raw[12:16] != b"IHDR"
    ):
        raise ValueError("Frozen source image is not a bounded PNG")
    width, height = struct.unpack(">II", raw[16:24])
    if not 1 <= width <= 2200 or not 1 <= height <= 2200:
        raise ValueError("Frozen source image exceeds its declared pixel bound")
    return width, height


@dataclass(frozen=True)
class FrozenImage:
    """Bind one read-only image capability to exact controller-derived bytes."""

    metadata: dict[str, Any]
    blob_directory: Path

    def read(self) -> bytes:
        """Authenticate frozen image bytes on each delivery without reading a live PDF."""
        key = self.metadata.get("png_sha256")
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("Frozen image requires an exact SHA-256 blob identity")
        assert_receipt_control_path(self.blob_directory.parent, "blobs/" + key)
        raw = read_regular_bytes(contained(self.blob_directory, key), MAX_IMAGE_BYTES)
        if digest(raw) != key or _dimensions(raw) != (
            self.metadata["width"],
            self.metadata["height"],
        ):
            raise ValueError("Frozen source-page image bytes changed")
        return raw


def attach_visual_evidence(
    record: dict[str, Any],
    documents: dict[str, dict[str, Any]],
    *,
    directory: Path,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, FrozenImage]:
    """Attach complete frozen companion renders to one review catalog, never source acceptance."""
    inventory = record["inventory"]
    companion = inventory.get("companion_pdf")
    if inventory["source_kind"] != "latex" or not companion:
        return {}
    if not has_complete_page_census(inventory):
        raise ValueError(
            "Companion visual review requires the complete trusted physical page census"
        )
    pages = inventory["pages"]
    if not 1 <= len(pages) <= MAX_VISUAL_PAGES or sorted(page["index"] for page in pages) != list(
        range(len(pages))
    ):
        raise ValueError("Companion visual review requires a bounded, exact physical page sequence")
    file = companion["file"]
    pdf_hash = record["source_files"][file]
    renderer = renderer_identity()
    binding = {
        "file": file,
        "pdf_sha256": pdf_hash,
        "physical_page_count": len(pages),
        "renderer": renderer,
        "flags": list(RENDER_FLAGS),
    }
    cache_key = identity(binding)
    store = Path(record["_directory"])
    cache_relative = "visual/" + cache_key + ".json"
    assert_receipt_control_path(store, cache_relative)
    cache = contained(store, cache_relative)
    if cache.exists():
        manifest = json.loads(read_regular_bytes(cache, 2 * 1024**2))
        if (
            not isinstance(manifest, dict)
            or manifest.get("binding") != binding
            or manifest.get("manifest_id")
            != identity({key: value for key, value in manifest.items() if key != "manifest_id"})
        ):
            raise ValueError("Frozen visual evidence manifest changed")
        rows = manifest.get("pages")
        if not isinstance(rows, list) or len(rows) != len(pages):
            raise ValueError("Frozen visual evidence omitted physical pages")
    else:
        work = directory.resolve() / uuid.uuid4().hex
        work.mkdir(parents=True, exist_ok=False, mode=0o700)
        frozen = work / "source.pdf"
        frozen.write_bytes(source_bytes(record, file))
        rows = []
        for index in range(len(pages)):
            page_directory = work / f"page-{index}"
            page_directory.mkdir(mode=0o700)
            raw = render_page(
                frozen,
                page_directory / "image",
                index,
                renderer,
                deadline=deadline,
                cancelled=cancelled,
            )
            width, height = _dimensions(raw)
            png_hash = put_blob(store / "blobs", raw)
            rows.append(
                {
                    "evidence_id": "page-image-"
                    + identity({"binding": binding, "index": index, "png_sha256": png_hash}),
                    "file": file,
                    "page_index": index,
                    "pdf_sha256": pdf_hash,
                    "png_sha256": png_hash,
                    "width": width,
                    "height": height,
                    "renderer": renderer,
                    "flags": list(RENDER_FLAGS),
                }
            )
        if renderer_identity() != renderer or digest(frozen.read_bytes()) != pdf_hash:
            raise ValueError("Renderer or frozen PDF changed during physical page rendering")
        manifest = {"schema_version": 1, "binding": binding, "pages": rows}
        manifest["manifest_id"] = identity(manifest)
        durable_json(cache, manifest)
    images = {}
    for index, row in enumerate(rows):
        if (
            not isinstance(row, dict)
            or row.get("file") != file
            or row.get("page_index") != index
            or row.get("pdf_sha256") != pdf_hash
            or row.get("renderer") != renderer
            or row.get("flags") != list(RENDER_FLAGS)
            or row.get("evidence_id")
            != "page-image-"
            + identity({"binding": binding, "index": index, "png_sha256": row.get("png_sha256")})
        ):
            raise ValueError("Frozen image does not match its source, renderer and physical page")
        image = FrozenImage(row, store / "blobs")
        image.read()
        images[row["evidence_id"]] = image
        documents[row["evidence_id"]] = dict(row)
    documents["source:" + file].update(
        rendered_page_delivery="read_source_image",
        page_images=rows,
        visual_manifest_id=manifest["manifest_id"],
    )
    for value in documents.values():
        region = value.get("region", {})
        if region.get("coordinate_space") == "pdf_page" and region.get("file") == file:
            index = region.get("page_index")
            if type(index) is int and 0 <= index < len(rows):
                value.update(
                    source_image_id=rows[index]["evidence_id"],
                    rendered_page_delivery="read_source_image",
                )
    return images


def required_page_images(dossier: dict[str, Any], documents: dict[str, dict[str, Any]]) -> set[str]:
    """Require each assigned companion physical page, including coalesced region members."""
    required = set()
    for region in dossier.get("regions", []):
        members = region.get("member_region_ids", [region["id"]])
        for key in members:
            evidence = documents.get("region:" + key, {})
            image = evidence.get("source_image_id")
            if image:
                required.add(image)
    return required
