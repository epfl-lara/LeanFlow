"""Pure local proof-context assembly for the Lean proof-context fallback path.

``_local_proof_context_payload`` reconstructs a proof-context payload from an
on-disk declaration slice (no MCP backend, no run state) and is used by
``lean_services.lean_proof_context`` whenever the managed proof-context backend
is unavailable, fails, or returns an empty declaration context.

It depends only on the stateless path-based declaration helpers in
``lean_declarations`` (plus stdlib), so it imports nothing from ``lean_services``
and introduces no import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from epflemma_cli.lean.lean_declarations import (
    _declaration_text_from_location,
    _find_declaration_entry,
    _split_declaration_statement_and_proof,
    _surrounding_declarations,
)


def _local_proof_context_payload(
    file_path: Path,
    theorem_id: str,
    *,
    degraded_reasons: list[str],
    scan_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    entry = _find_declaration_entry(file_path, theorem_id)
    if not entry:
        return None
    theorem_name = str(entry.get("name", "") or theorem_id).strip()
    theorem = dict(scan_payload.get("theorem") or {}) if isinstance(scan_payload, Mapping) else {}
    location = (
        dict(theorem.get("location") or {}) if isinstance(theorem.get("location"), Mapping) else {}
    )
    local_text = _declaration_text_from_location(file_path, location) if location else ""
    if not local_text:
        local_text = str(entry.get("text", "") or "")
    statement, proof = _split_declaration_statement_and_proof(local_text)
    metadata = {
        "fallback_source": "local-declaration-slice",
        "declaration_kind": str(entry.get("kind", "") or theorem.get("kind", "")),
        "line": int(entry.get("line", 0) or 0),
        "end_line": int(entry.get("end_line", 0) or 0),
        "scan_theorem": (
            dict(scan_payload or {}) if isinstance(scan_payload, Mapping) and scan_payload else {}
        ),
    }
    if location:
        metadata["location"] = location
    return {
        "success": True,
        "status": "local-fallback",
        "backend_tool": "local-declaration-slice",
        "degraded_reasons": list(dict.fromkeys(degraded_reasons)),
        "file_path": str(file_path),
        "theorem_id": theorem_name,
        "theorem_statement": statement,
        "original_proof": proof,
        "hypotheses": [],
        "in_scope": _surrounding_declarations(file_path, theorem_name),
        "namespace": theorem_name.rsplit(".", 1)[0] if "." in theorem_name else "",
        "similar_proofs": [],
        "metadata": metadata,
        "timing": {},
    }
