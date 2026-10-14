"""Prepare frozen formalization guidance without scaffolding canonical project files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization import lean_build
from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from leanflow_cli.formalization.evidence import durable_json
from leanflow_cli.formalization.formalization_models import (
    FormalizationDocumentContext,
    FormalizationDocumentError,
    _FormalizationDocumentSelection,
)
from leanflow_cli.formalization.lean_layout import resolve_layout
from leanflow_cli.formalization.records import create_document, document_directory, source_bytes


def author_blocking_diagnostics(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Permit preparatory drafting with a frozen companion, never source acceptance.

    Only missing figure/bibliography assets can be deferred. Their original
    blocking diagnostics and all coverage remain unchanged for the final gate.
    """
    companion = inventory.get("companion_pdf") or {}
    file = companion.get("file", "")
    metadata = inventory.get("files", {}).get(file, {})
    paired = (
        inventory.get("source_kind") == "latex"
        and companion.get("selection") == "explicit"
        and isinstance(file, str)
        and file.lower().endswith(".pdf")
        and metadata.get("encoding") == "binary"
        and bool(companion.get("sha256"))
        and companion["sha256"] == metadata.get("sha256")
    )
    return [
        row
        for row in inventory.get("diagnostics", [])
        if row.get("blocking")
        and not (
            paired
            and row.get("code") == "missing_support_file"
            and row.get("support_command") in {"includegraphics", "bibliography", "addbibresource"}
            and isinstance(row.get("support_target"), str)
            and row["support_target"].strip()
        )
    ]


def draft_source_guidance(inventory: dict[str, Any]) -> str:
    """Describe unresolved companion-backed assets without granting a repair or waiver."""
    strict = author_blocking_diagnostics(inventory)
    deferred = [
        row for row in inventory.get("diagnostics", []) if row.get("blocking") and row not in strict
    ]
    if not deferred:
        return ""
    issues = "\n".join(f"- {row['id']}: {row['message']}" for row in deferred)
    return f"""DRAFT PREPARATION ONLY — unresolved source assets
The explicitly paired frozen PDF permits preparatory work despite these missing
figure or bibliography files. It does not resolve them or verify correspondence:
{issues}
All source diagnostics and coverage remain pending. Acceptance, publication and
prover handoff are blocked until the source issues are repaired and independently
reviewed. Read the complete TeX and companion pages; record ambiguities and do not
invent figure content, omit mathematics, exclude regions, or claim user approval.
If these gaps prevent a faithful complete proposal, save a useful representative
draft in the allocated module subtree and setup/blocker notes in scratch, then
return exactly {{"status":"blocked","reason":"concrete unresolved source issues"}}.
Do not submit a falsely complete proposal or keep retrying unchanged source gaps.
The controller ends this draft-only run after one completed author turn; repairing
the original source and restarting is required before any acceptance attempt.
"""


def draft_source_rejection(inventory: dict[str, Any]) -> dict[str, Any] | None:
    """Return controller-owned rejection for the narrow companion-backed draft exception."""
    strict = author_blocking_diagnostics(inventory)
    deferred = [
        row for row in inventory.get("diagnostics", []) if row.get("blocking") and row not in strict
    ]
    if not deferred:
        return None
    return {
        "accepted": False,
        "status": "source_blocked",
        "error_code": "source_repair_required",
        "error": "Unresolved source assets prevent formalization acceptance: "
        + "; ".join(row["message"] for row in deferred),
        "source_diagnostics": [row["id"] for row in deferred],
    }


def preflight_dependencies(root: Path, document_id: str = "preflight") -> dict[str, Any]:
    """Apply the builder's read-only admission before starting paid formalization work."""
    try:
        inventory = dependency_inputs(root)
        layout = resolve_layout(root, document_id)
        if not layout.get("accepted"):
            raise ValueError(str(layout.get("error", "Unsupported project layout")))
        lean_build.validate_build_inputs(root, layout, inventory=inventory)
    except (OSError, ValueError) as error:
        raise FormalizationDocumentError(
            "Formalization dependency preflight failed: " + str(error)
        ) from error
    return layout


def prepare_intake(
    root: Path, selection: _FormalizationDocumentSelection
) -> FormalizationDocumentContext:
    """Freeze source authority and allocate a real Lake-owned prospective module tree."""
    record = create_document(root, selection.source_path)
    blockers = [row["message"] for row in author_blocking_diagnostics(record["inventory"])]
    if blockers:
        raise FormalizationDocumentError(
            "Source extraction needs repair before starting formalization: " + "; ".join(blockers)
        )
    layout = preflight_dependencies(root, record["document_id"])
    directory = document_directory(root, record["document_id"]) / "intake" / record["revision_id"]
    directory.mkdir(parents=True, exist_ok=True)
    context_path, manifest_path = directory / "context.md", directory / "manifest.json"
    extracted_path, skill_path = directory / "extracted.txt", directory / "SKILL.md"
    blueprint_path = root / "formalization" / record["document_id"] / "Blueprint.md"
    text: list[str] = []
    for name in record["source_files"]:
        if record["inventory"]["source_kind"] == "pdf":
            text.extend(
                f"Physical PDF page {p['index'] + 1}\n{p.get('text', '')}"
                for p in record["inventory"].get("pages", [])
            )
        else:
            encoding = record["inventory"]["files"][name].get("encoding", "utf-8")
            if encoding == "binary":
                text.append(
                    f"SUPPORTING BINARY ARTIFACT {name}: preserved in the source bundle; inspect visually if mathematically relevant."
                )
            else:
                text.append(f"SOURCE FILE {name}\n" + source_bytes(record, name).decode(encoding))
    if record["inventory"].get("companion_pdf"):
        text.extend(
            f"COMPANION PDF {page['file']} — physical page {page['index'] + 1}\n{page.get('text', '')}"
            for page in record["inventory"].get("pages", [])
        )
    extracted_path.write_text("\n\n".join(text), encoding="utf-8")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "document_id": record["document_id"],
        "revision_id": record["revision_id"],
        "inventory": record["inventory"],
        "layout": layout,
        "units": record.get("units", []),
        "source_bundle": record["source_bundle"],
    }
    durable_json(manifest_path, manifest)
    guidance = f"""# Formalization assignment

Document: {selection.source_relative}
Frozen base revision: {record['revision_id']}
Read the complete source manifest and original extracted files. Every selected region
must be audited, including prose outside theorem environments. Blocking extraction
diagnostics require source repair; never hide uncertainty, omit candidates, or narrow
scope. If an unrecognized statement is found, report it for inventory repair.

{draft_source_guidance(record['inventory'])}
The evaluated Lake library is {layout['library']}. Put modules only in
`{layout['owned_prefix']}/` and its aggregator `{layout['aggregator']}`. The aggregator
must import every generated module. LeanFlow registers the root import after checking.
Preserve all original project files and dependency configuration. Reuse exact pinned
library declarations where appropriate; a zero-new-declaration translation is valid.
Before expanding the whole paper, check a representative slice: a shared definition,
a dependent statement, a library reuse or bridge, and a named proof obligation where
applicable. Record the representation choices and the slice's Lean feedback in
setup_notes. Revisit those choices if later statements reveal a poor setup.
Implement definitions, instances and constructions completely. Named Prop theorem
proofs may remain pending; complete proofs are also welcome. A construction using a
named pending existence theorem must expose that dependency. Never use data holes,
dummy inhabitants, invented hypotheses, or new axioms to make the build pass.

Before submit_files, write `.leanflow-job/formalization-proposal.json`:
{{"schema_version":1,"base_revision_id":"{record['revision_id']}",
 "setup_notes":"representation and shared definition choices",
 "units":[{{"id":"unit-id","required_source_ids":["manifest source ID"],
 "declarations":[{{"file":"project-relative Lean file","name":"exact fully qualified name"}}],
 "representation_note":"faithfulness and split/merge rationale","depends_on_units":[]}}],
 "coverage":[{{"region_id":"every manifest region ID","disposition":"item",
 "item_ids":["source ID"],"reason":"specific coverage evidence"}}]}}
For direct library reuse omit file and provide exact module imports, for example
`imports:["Init"]` for `Nat.add_comm`. A helper may omit explicit required_source_ids
only with a justification; pending helpers must inherit source provenance through
compiled dependencies or provide it explicitly. Region dispositions are item, supporting_context, non_mathematical,
or a preserved explicit exclusion. Every source item except a user-excluded item must
be mapped, and every region must occur exactly once. The independent reviewer must
pass each item and the whole document. Author self-approval never satisfies a gate.
"""
    context_path.write_text(guidance, encoding="utf-8")
    skill_path.write_text(
        "---\nname: formalization-source\ndescription: Frozen source and exact prover handoff guidance.\n---\n\n"
        + guidance,
        encoding="utf-8",
    )
    metadata = {
        **selection.discovery_metadata,
        "document_id": record["document_id"],
        "revision_id": record["revision_id"],
        "layout": layout,
        "source_bundle": record["source_bundle"],
        "inventory": record["inventory"],
    }
    return FormalizationDocumentContext(
        source_path=selection.source_path,
        source_relative=selection.source_relative,
        source_kind=selection.source_kind,
        context_path=context_path,
        manifest_path=manifest_path,
        extracted_text_path=extracted_path,
        blueprint_path=blueprint_path,
        blueprint_skill_path=skill_path,
        target_lean_path=root / layout["aggregator"],
        target_lean_relative=layout["aggregator"],
        metadata=metadata,
    )
