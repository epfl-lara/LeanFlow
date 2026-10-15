"""Bind cached reviews to frozen evidence and retain unchanged semantic rejections."""

from __future__ import annotations

import re
from typing import Any

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.review_dossier import evidence_documents
from leanflow_cli.workflows.prover.source import lean_lexical_regions


def _lean_evidence(source: str) -> dict[str, Any]:
    """Drop comment width and blank lines while preserving literals and code indentation."""
    try:
        regions = list(lean_lexical_regions(source))
    except ValueError:
        # Context files such as lakefile.lean can use syntax outside the draft
        # admission subset. Their exact bytes remain conservative cache inputs.
        return {"raw": source}
    parts: list[str] = []
    literals: list[str] = []
    previous = 0
    line_has_code = False
    for start, end, comment in regions:
        prefix = source[previous:start]
        parts.append(prefix)
        line_has_code = (
            bool(prefix.rsplit("\n", 1)[-1].strip())
            if "\n" in prefix
            else line_has_code or bool(prefix.strip())
        )
        if comment:
            newlines = source[start:end].count("\n")
            parts.append((" " if line_has_code else "") + "\n" * newlines)
            if newlines:
                line_has_code = False
        else:
            parts.append(f"__leanflow_literal_{len(literals)}__")
            literals.append(source[start:end])
            line_has_code = True
        previous = end
    parts.append(source[previous:])
    lines = []
    for line in "".join(parts).splitlines():
        if line.strip():
            indentation = line[: len(line) - len(line.lstrip(" \t"))]
            lines.append(indentation + re.sub(r"[ \t]+", " ", line[len(indentation) :]).rstrip())
    return {
        "code": "\n".join(lines),
        "literals": literals,
    }


def _units_without_locations(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep source relationships and declaration identities without compiler coordinates."""
    return [
        {
            **unit,
            "declarations": [
                # Definition bodies remain bound by the compiled declarations
                # below; theorem bodies can contain source-position sorry labels.
                {
                    key: value
                    for key, value in ref.items()
                    if key
                    not in {
                        "line_start",
                        "line_end",
                        "source_range",
                        "declaration_span",
                        "body_hash",
                    }
                }
                for ref in unit["declarations"]
            ],
        }
        for unit in units
    ]


def rejection_key(
    record: dict[str, Any],
    dossier: dict[str, Any],
    configuration: dict[str, str],
    *,
    documents: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Ignore Lean comments, interior spacing and locations for a negative verdict.

    Positive reuse still requires exact module bytes. Negative reuse conservatively
    binds the whole project, imports, literal values, source interpretation and
    compiled expressions; unrelated comments cannot purchase another opinion.
    """
    if documents is None:
        documents = evidence_documents(record)
    lean = {
        name: _lean_evidence(value["text"])
        for name, value in documents.items()
        if name.startswith("lean:")
    }
    declarations = [
        {
            key: row[key]
            for key in (
                "name",
                "type",
                "type_hash",
                "body_hash",
                "kind",
                "pending",
                "depends_on",
                "direct_dependencies",
                "axioms",
                "levels",
                "file",
                "module",
            )
            if key in row and (key != "body_hash" or row.get("kind") != "theorem")
        }
        for row in record["checks"]["declarations"]
    ]
    return identity(
        {
            "schema": "semantic-rejection-v3",
            "configuration": configuration,
            "inventory": record["inventory"],
            "source_bundle": record["source_bundle"],
            "environment_id": record["environment_id"],
            "policy_id": record["policy_id"],
            "layout": record["layout"],
            "units": _units_without_locations(record["units"]),
            "lean": lean,
            "declarations": declarations,
            "setup_notes": record.get("setup_notes", ""),
            **(
                {"source_reconciliation": record["source_reconciliation"]}
                if record.get("source_reconciliation")
                else {}
            ),
            "phase": dossier["phase"],
            "controller_scope": dossier.get("controller_scope"),
            "scope_evidence": documents.get("controller-scope"),
            "full_inventory": documents.get("full-inventory"),
            "batch": [u["id"] for u in dossier["units"]],
            "regions": [r["id"] for r in dossier["regions"]],
        }
    )
