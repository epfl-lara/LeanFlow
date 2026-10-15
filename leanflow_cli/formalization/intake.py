"""Prepare frozen formalization guidance without scaffolding canonical project files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization import lean_build
from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from leanflow_cli.formalization.evidence import durable_json, identity
from leanflow_cli.formalization.formalization_models import (
    FormalizationDocumentContext,
    FormalizationDocumentError,
    _FormalizationDocumentSelection,
)
from leanflow_cli.formalization.lean_layout import generation_layout, resolve_layout
from leanflow_cli.formalization.records import (
    create_document,
    document_directory,
    document_lock,
    load_document,
    new_revision,
    rehydrate_document,
    save_revision,
    source_bytes,
)
from leanflow_cli.formalization.source_reconciliation import (
    assert_current_extraction,
    compact_requirements,
    lexical_admission,
    reconciliation_requirements,
)
from leanflow_cli.formalization.source_review_regions import coalesce_regions, compact_region


def author_blocking_diagnostics(
    inventory: dict[str, Any], *, source_draft_only: bool = False
) -> list[dict[str, Any]]:
    """Permit preparatory drafting with a frozen companion, never source acceptance.

    Explicit drafting also admits known source uncertainty with authentic frozen
    spans. Every original blocker and coverage region remains pending for the final gate.
    """
    from leanflow_cli.formalization.source_draft import draft_policy

    if not source_draft_only:
        try:
            assert_current_extraction(inventory)
        except ValueError as error:
            return [
                {
                    "id": "source-extraction-outdated",
                    "code": "outdated_extraction_policy",
                    "blocking": True,
                    "message": str(error),
                }
            ]
    policy = draft_policy(inventory, explicit=source_draft_only)
    strict = list(policy["strict"])
    if policy["draft_only"]:
        return strict
    hard = lexical_admission(inventory)
    return [row for row in strict if row in hard]


def draft_source_guidance(inventory: dict[str, Any], *, source_draft_only: bool = False) -> str:
    """Describe unresolved companion-backed assets without granting a repair or waiver."""
    from leanflow_cli.formalization.source_draft import draft_policy

    policy = draft_policy(inventory, explicit=source_draft_only)
    deferred = policy["deferred"]
    if not policy["draft_only"]:
        return ""
    issues = "\n".join(f"- {row['id']}: {row['message']}" for row in deferred)
    return f"""DRAFT PREPARATION ONLY — source uncertainty and unresolved assets
The explicitly paired frozen PDF permits preparatory work. It does not resolve
TeX execution or asset uncertainty, or verify correspondence with the source:
{issues}
All source diagnostics and coverage remain pending. Acceptance, publication and
prover handoff are blocked until the source issues are repaired and independently
reviewed. Read the complete TeX and companion pages; record ambiguities and do not
invent figure content, omit mathematics, exclude regions, or claim user approval.
Loader or inherited execution uncertainty affects the complete source interpretation;
do not claim that any extracted candidate is certainly active or complete.
Keep both conditional branches and all candidate scopes. Save a useful representative
draft only; do not expand beyond the prepared representative slice. Write provisional
setup notes to `.leanflow-job/setup-notes.md` and source blockers to
`.leanflow-job/blockers.md`. These files and the model report remain unaccepted.
If these gaps prevent a faithful complete proposal, save a useful representative
draft in the allocated module subtree and setup/blocker notes in scratch, then
return exactly {{"status":"blocked","reason":"concrete unresolved source issues"}}.
Do not submit a falsely complete proposal or keep retrying unchanged source gaps.
The controller ends this draft-only campaign after one completed author turn; resume
only displays the retained preview. Source reconciliation and a fresh ordinary
formalization campaign are required before any acceptance attempt.
"""


def draft_source_rejection(
    inventory: dict[str, Any], *, source_draft_only: bool = False
) -> dict[str, Any] | None:
    """Return controller-owned rejection for the narrow companion-backed draft exception."""
    from leanflow_cli.formalization.source_draft import draft_policy

    policy = draft_policy(inventory, explicit=source_draft_only)
    deferred = policy["deferred"]
    if not policy["draft_only"]:
        return None
    return {
        "accepted": False,
        "status": "source_blocked",
        "error_code": "source_repair_required",
        "error": "Unresolved source assets or explicit draft-only intent prevent formalization acceptance: "
        + "; ".join(row["message"] for row in deferred),
        "source_diagnostics": [row["id"] for row in deferred],
    }


def preflight_dependencies(root: Path, document_id: str = "preflight") -> dict[str, Any]:
    """Apply the builder's read-only admission before starting paid formalization work."""
    try:
        from leanflow_cli.formalization.preparation_transaction import assert_no_pending_preparation

        assert_no_pending_preparation(root)
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
    root: Path,
    selection: _FormalizationDocumentSelection,
    *,
    require_preparation: bool = False,
    source_draft_only: bool = False,
) -> FormalizationDocumentContext:
    """Freeze source authority and allocate a real Lake-owned prospective module tree."""
    record = create_document(root, selection.source_path)
    from leanflow_cli.formalization.source_draft import assert_unaccepted_draft

    if draft_source_rejection(record["inventory"], source_draft_only=source_draft_only) is not None:
        assert_unaccepted_draft(record)
    checked_plan: dict[str, Any] | None = None
    if require_preparation:
        from leanflow_cli.formalization.preparation import assert_preparation_current

        receipt = assert_preparation_current(root)
        if receipt["source_bundle"] != record["source_bundle"]:
            raise FormalizationDocumentError(
                "Prepared source changed; estimate and check setup again"
            )
        fields = {
            "workflow_protocol": 2,
            "preparation_id": receipt["preparation_id"],
            "preparation_plan_id": receipt["preparation_plan_id"],
            "slice_source_ids": receipt["plan"]["slice_source_ids"],
        }
        if any(record.get(key) != value for key, value in fields.items()):
            with document_lock(root, record["document_id"]) as owner:
                current = load_document(root, record["document_id"])
                if current["revision_id"] != record["revision_id"]:
                    raise FormalizationDocumentError("Source selection changed during preparation")
                rehydrate_document(current, owner)
                record = new_revision(current)
                record.update(fields)
                record = save_revision(owner, record)
    from leanflow_cli.formalization.inventory_authority import complete_inventory

    inventory = complete_inventory(record)
    if record.get("workflow_protocol") == 2:
        from leanflow_cli.formalization.preparation_assignment import checked_preparation_assignment

        checked_plan = checked_preparation_assignment(root, record)
    blockers = [
        row["message"]
        for row in author_blocking_diagnostics(inventory, source_draft_only=source_draft_only)
    ]
    if blockers:
        raise FormalizationDocumentError(
            "Source extraction needs repair before starting formalization: " + "; ".join(blockers)
        )
    layout = generation_layout(preflight_dependencies(root, record["document_id"]), record)
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
    requirements = reconciliation_requirements(record)
    from leanflow_cli.formalization.source_draft import draft_policy

    draft_only = draft_policy(inventory, explicit=source_draft_only)["draft_only"]
    groups = coalesce_regions(inventory["regions"])
    partition = {
        "canonical_inventory": inventory,
        "groups": groups,
        "authority": "complete ordered source census; grouped view never narrows coverage",
        "source_reconciliation_requirements": requirements,
    }
    partition_path = directory / "source-region-partition.json"
    durable_json(partition_path, partition)
    grouped_inventory = {
        **inventory,
        "regions": [compact_region(group) for group in groups],
        "canonical_region_count": len(inventory["regions"]),
        "region_authority": "exact-contiguous-canonical-partition",
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "document_id": record["document_id"],
        "revision_id": record["revision_id"],
        "inventory": inventory if draft_only else grouped_inventory,
        "source_region_partition": {
            "path": str(partition_path),
            "sha256": identity(partition),
            "canonical_region_count": len(inventory["regions"]),
            "group_count": len(groups),
        },
        "layout": layout,
        "units": record.get("units", []),
        "source_bundle": record["source_bundle"],
        "source_reconciliation_requirements": compact_requirements(requirements),
        **({"checked_preparation_plan": checked_plan} if checked_plan else {}),
        **{
            key: record[key]
            for key in (
                "workflow_protocol",
                "preparation_id",
                "preparation_plan_id",
                "slice_source_ids",
            )
            if key in record
        },
    }
    durable_json(manifest_path, manifest)
    guidance = f"""# Formalization assignment

Document: {selection.source_relative}
Frozen base revision: {record['revision_id']}
Read the complete source manifest and original extracted files. Every selected region
must be audited, including prose outside theorem environments. Blocking extraction
diagnostics require source repair or the manifest's exact lexical reconciliation proposal;
hard execution/asset/expansion blockers remain source repairs. Never hide uncertainty, omit candidates, or narrow
scope. If an unrecognized statement is found, report it for inventory repair.
The manifest uses bounded contiguous review groups. Their exact ordered canonical
members remain unmodified in source_region_partition; use that audit file when exact
member provenance is needed. Submit coverage for every group ID exactly once, or
every canonical region ID exactly once; never mix the coordinate systems or omit a
member. Review all mathematical source content, including every known clause.

{draft_source_guidance(inventory, source_draft_only=source_draft_only)}
Read the complete source manifest, including checked_preparation_plan when present.
Its exact package pins and imports have infrastructure checks. Its prerequisite
candidate names and representation notes are estimates: inspect and compile reuse,
then obtain independent source review before they can pass a translation gate.
The evaluated Lake library is {layout['library']}. Put modules only in
`{layout['owned_prefix']}/` and its aggregator `{layout['aggregator']}`. The aggregator
must import every generated module. LeanFlow registers the root import after checking.
Preserve all original project files and dependency configuration. Reuse exact pinned
library declarations where appropriate; a zero-new-declaration translation is valid.
Place new declarations in namespace `{layout['module_prefix']}` so a new reviewed
translation can coexist with preserved earlier generations. Reuse existing complete
declarations directly where faithful; never overwrite old translation modules.
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
Each source item may contain explicitly registered known_obligations. Every unit
using that source must supply clause_dispositions for each known obligation:
{{"obligation_id":"exact id","disposition":"represented/pending_proof/omitted/specialized/unresolved",
"declaration_names":["exact mapped name"],"rationale":"specific clause evidence"}}.
Omitted, specialized or unresolved clauses block full-item acceptance even when
representation_note discloses them. pending_proof must name a real Prop theorem
proof slot expressing the missing clause with complete computational definitions
and types; it does not authorize data holes or dropped requirements. The known-clause
checklist is not automatic discovery: preserve and review every source qualifier.
When source_reconciliation_requirements lists lexical diagnostics, the proposal must
include source_reconciliation={{"requirements_id":"exact requirements ID","entries":[
{{"diagnostic_id":"every exact eligible ID","interpretation":"literal lexical reading",
"reason":"specific source evidence"}}]}}. Preserve every conditional alternative and
complete context. This proposes an interpretation for normal independent coverage
and coherence review; it cannot certify package/loader purity, resolve a hard effect,
exclude a source branch or grant mathematical approval. Never claim actual TeX execution.
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
        "inventory": inventory,
        "source_draft_only": source_draft_only,
        **{
            key: record[key]
            for key in (
                "workflow_protocol",
                "preparation_id",
                "preparation_plan_id",
                "slice_source_ids",
            )
            if key in record
        },
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
