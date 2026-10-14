"""Index complete mathematical evidence separately from the unabridged controller audit."""

from __future__ import annotations

from typing import Any

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.review_transport import _json_text


def declaration_index(checks: dict[str, Any]) -> dict[str, Any]:
    """Link every exact compiled row without duplicating bodies and kernel replay inventories."""
    serialized = _json_text(checks)
    return {
        "schema_version": 1,
        "role": "complete compiled declaration registry index",
        "declarations": [
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key
                    in {
                        "name",
                        "module",
                        "kind",
                        "file",
                        "declaration_origin",
                        "type_hash",
                        "body_hash",
                        "pending",
                        "conditional",
                        "depends_on",
                    }
                },
                "evidence_id": "declaration:" + row["name"],
            }
            for row in checks["declarations"]
        ],
        "library_declarations": [
            {
                "name": row["name"],
                "module": row["module"],
                "role": row.get("role", "dependency"),
                "semantic_surface": row.get("semantic_surface", True),
                "evidence_id": "library-declaration:" + row["name"],
            }
            for row in checks.get("library_declarations", [])
        ],
        "controller_audit": {
            "evidence_id": "controller-checks",
            "sha256": identity(checks),
            "characters": len(serialized),
            "utf8_bytes": len(serialized.encode("utf-8")),
        },
        "review_requirement": (
            "Read complete exact declaration rows and the referenced mathematical "
            "library/local definition semantic surface using these evidence IDs. "
            "The complete controller audit remains available through controller-checks; "
            "its duplicate profiles, proof obligations and imported-module inventory "
            "are compilation provenance, not additional source mathematics."
        ),
    }


def compiler_base_policy(checks: dict[str, Any]) -> dict[str, Any]:
    """Describe the inspector's existing pinned-base boundary without inventing semantic rows."""
    return {
        "authority": "Existing pinned compiler/base trust boundary",
        "trusted_module_count": len(checks.get("kernel_replay", {}).get("trusted_modules", [])),
        "controller_audit_evidence_id": "controller-checks",
        "scope": (
            "The kernel inspector imports the recorded pinned compiler base as trusted "
            "and omits its constants from noncompiler library semantic rows. The full "
            "trusted-module/import inventory is retained in controller-checks. "
            "This does not claim source/build correspondence or individual compiler-base "
            "definition inspection. All referenced noncompiler library and local "
            "mathematical definitions still require their actual compiled semantic rows. "
            "Only request library-declaration IDs present in the evidence catalog; "
            "block if any required mathematical meaning cannot be assessed."
        ),
    }
