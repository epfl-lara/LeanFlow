"""Describe immutable source, Lean and draft viewer artifacts without currentness walks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.records import source_artifact_id


def _artifacts(
    record: dict[str, Any],
    proof_receipt: dict[str, Any] | None = None,
    draft_preview: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    result = [
        {
            "id": source_artifact_id(record, file),
            "path": file,
            "kind": "pdf" if file.lower().endswith(".pdf") else "source",
            "sha256": key,
            "title": Path(file).name,
            "origin": "source",
        }
        for file, key in record["source_files"].items()
    ]
    result.extend(
        {
            "id": ("proof-lean-" if proof_receipt else "lean-")
            + identity({"file": file, "sha256": key}),
            "path": file,
            "kind": "lean",
            "sha256": key,
            "title": Path(file).name,
            "origin": "proof" if proof_receipt else "lean",
        }
        for file, original in record.get("lean_files", {}).items()
        for key in [(proof_receipt or {}).get("after", {}).get(file, original)]
    )
    result.extend(
        {
            "id": "context-lean-" + identity({"file": file, "sha256": key}),
            "path": file,
            "kind": "lean",
            "sha256": key,
            "title": Path(file).name,
            "origin": "context",
        }
        for file, key in record.get("context_lean_files", {}).items()
        if file not in record.get("lean_files", {})
    )
    result.extend({**row, "origin": "draft"} for row in (draft_preview or {}).get("artifacts", []))
    return result


def _display_preview(
    root: Path, record: dict[str, Any], *, verify_current: bool = True
) -> tuple[dict[str, Any] | None, str]:
    """Isolate optional preview corruption so original source and other documents stay inspectable."""
    from leanflow_cli.formalization.source_draft import load_preview

    try:
        return load_preview(root, record, verify_current=verify_current), ""
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        return None, "Draft preview unavailable: " + str(error)
