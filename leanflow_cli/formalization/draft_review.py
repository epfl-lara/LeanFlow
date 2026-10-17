"""Review complete best-effort drafts without granting source, publication or proof authority."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization.clause_obligations import obligation_registry
from leanflow_cli.formalization.evidence import contained, digest, identifier, put_blob
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.review import review_revision
from leanflow_cli.formalization.source_draft_artifacts import MAX_DRAFT_FILE_BYTES, read_regular


def _proposal(value: dict[str, Any] | None) -> dict[str, Any]:
    """Retain a bounded authored proposal as evidence rather than as an approved mapping."""
    if value is None:
        return {}
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(raw.encode()) > MAX_DRAFT_FILE_BYTES:
        raise ValueError("Advisory proposal exceeds the private draft artifact limit")
    if not isinstance(value, dict):
        raise ValueError("Advisory proposal must be a JSON object")
    return json.loads(raw)


def _references(
    value: Any, registry: dict[str, dict[str, Any]], files: set[str]
) -> list[dict[str, Any]]:
    """Replace authored declaration claims with available exact compiled references."""
    result: dict[str, dict[str, Any]] = {}
    for reference in value if isinstance(value, list) else []:
        row = (
            registry.get(reference["name"])
            if isinstance(reference, dict) and isinstance(reference.get("name"), str)
            else None
        )
        if row is None or (row.get("file") and row["file"] not in files):
            continue
        if reference.get("file") and reference["file"] != row.get("file"):
            continue
        result[row["name"]] = {
            key: copy.deepcopy(row[key])
            for key in ("name", "file", "module", "type", "type_hash", "body_hash", "conditional")
            if key in row
        }
    return list(result.values())


def _units(
    record: dict[str, Any], proposal: dict[str, Any], registry: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Keep every included source visible, marking absent or provisional mappings unresolved."""
    included = {row["id"] for row in record["inventory"]["items"] if row.get("scope") != "excluded"}
    corrections = proposal.get("corrections", [])
    provisional = (
        {
            row["source_item_id"]
            for row in corrections
            if isinstance(row, dict) and isinstance(row.get("source_item_id"), str)
        }
        if isinstance(corrections, list)
        else included
    )
    provisional = {key for key in provisional if isinstance(key, str)}
    unresolved: set[str] = set()
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    covered: set[str] = set()
    files = set(record.get("lean_files", {})) | set(record.get("context_lean_files", {}))
    rows = proposal.get("units", [])
    if not isinstance(rows, list) or len(rows) > 512:
        rows = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict) or not isinstance(raw.get("required_source_ids"), list):
            continue
        sources = sorted(
            {key for key in raw["required_source_ids"] if isinstance(key, str)} & included
        )
        if not sources:
            continue
        try:
            key = identifier(raw.get("id", ""))
        except ValueError:
            key = f"draft-unit-{index}"
        while key in seen:
            key += "-draft"
        seen.add(key)
        refs = _references(raw.get("declarations"), registry, files)
        names = {row["name"] for row in refs}
        dispositions = raw.get("clause_dispositions", [])
        dispositions = dispositions if isinstance(dispositions, list) else []
        clauses = []
        for clause in obligation_registry(record, set(sources)).values():
            matches = [
                row
                for row in dispositions
                if isinstance(row, dict) and row.get("obligation_id") == clause["id"]
            ]
            authored = matches[0] if len(matches) == 1 else {}
            mapped = authored.get("declaration_names", [])
            mapped = (
                list(
                    dict.fromkeys(
                        name for name in mapped if isinstance(name, str) and name in names
                    )
                )
                if isinstance(mapped, list)
                else []
            )
            disposition = authored.get("disposition")
            valid = isinstance(disposition, str) and disposition in {
                "represented",
                "pending_proof",
                "omitted",
                "specialized",
                "unresolved",
            }
            pending = any(
                registry[name].get("kind") == "theorem"
                and registry[name].get("is_prop") is True
                and registry[name].get("pending") is True
                and all(
                    registry[name].get(flag) is False
                    for flag in ("data_sorry", "direct_type_sorry", "type_untracked_sorry")
                )
                for name in mapped
            )
            if (
                not valid
                or clause["source_id"] in provisional
                or (disposition in {"represented", "pending_proof"} and not mapped)
                or (disposition == "pending_proof" and not pending)
                or proposal.get("base_revision_id") != record["revision_id"]
            ):
                disposition = "unresolved"
                rationale = "Controller advisory projection: missing, provisional or unbound clause mapping; human clarification is optional."
            else:
                rationale = (
                    str(authored.get("rationale", ""))[:2048].strip()
                    or "Authored draft clause mapping; independent assessment remains advisory."
                )
            if disposition in {"omitted", "specialized", "unresolved"}:
                unresolved.add(clause["source_id"])
            clauses.append(
                {
                    "obligation_id": clause["id"],
                    "disposition": disposition,
                    "declaration_names": mapped,
                    "rationale": rationale,
                }
            )
        if (
            not refs
            or set(sources) & provisional
            or proposal.get("base_revision_id") != record["revision_id"]
        ):
            unresolved.update(sources)
        units.append(
            {
                "id": key,
                "required_source_ids": sources,
                "declarations": refs,
                "representation_note": "ADVISORY ONLY: "
                + str(raw.get("representation_note", ""))[:4096],
                "depends_on_units": [],
                "clause_dispositions": clauses,
            }
        )
        covered.update(sources)
    for index, source_id in enumerate(sorted(included - covered)):
        key = f"unmapped-draft-source-{index}"
        while key in seen:
            key += "-unmapped"
        seen.add(key)
        units.append(
            {
                "id": key,
                "required_source_ids": [source_id],
                "declarations": [],
                "representation_note": "Controller advisory projection: no usable authored Lean mapping was supplied; this source is still required and remains unresolved.",
                "depends_on_units": [],
                "clause_dispositions": [
                    {
                        "obligation_id": clause["id"],
                        "disposition": "unresolved",
                        "declaration_names": [],
                        "rationale": "No usable authored mapping; retain the complete original obligation for optional clarification.",
                    }
                    for clause in obligation_registry(record, {source_id}).values()
                ],
            }
        )
        unresolved.add(source_id)
    return units, unresolved


def build_advisory_subject(
    record: dict[str, Any],
    proposal: dict[str, Any] | None,
    checks: dict[str, Any],
    *,
    directory: Path,
) -> dict[str, Any]:
    """Freeze a private full-source review subject without applying corrections or slice approval."""
    authored = _proposal(proposal)
    result = copy.deepcopy(record)
    result["inventory"] = copy.deepcopy(complete_inventory(record))
    result["full_inventory"] = copy.deepcopy(result["inventory"])
    result["inventory"].update(audit_complete=False, slice_audit_complete=False)
    result.pop("workflow_protocol", None)
    result.pop("controller_review_scope", None)
    result.pop("review_policy", None)
    result.update(
        proposal_phase="complete",
        corrections=[],
        correction_authorities=[],
        reviews=[],
        handoff={},
        status="best_effort_advisory",
    )
    result["checks"] = copy.deepcopy(checks)
    result["checks"].update(accepted=False, correction_evidence=[])
    registry = {
        row["name"]: row
        for row in checks.get("declarations", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    result["units"], unresolved = _units(result, authored, registry)
    context = {
        "authority": "unaccepted-best-effort-advisory-only",
        "accepted": False,
        "publication_authority": False,
        "proof_acceptance": False,
        "proof_authority": False,
        "original_revision_id": record["revision_id"],
        "kernel_checks_accepted": checks.get("accepted") is True,
        "unresolved_source_ids": sorted(unresolved),
        "authored_proposal": authored,
        "original_correction_authorities": copy.deepcopy(record.get("correction_authorities", [])),
        "original_corrections": copy.deepcopy(record.get("corrections", [])),
    }
    result["checks"]["draft_advisory"] = context
    result["setup_notes"] = (
        str(record.get("setup_notes", ""))
        + "\nADVISORY BEST-EFFORT DRAFT: assess every original source "
        "item and region. Ambiguities are optional follow-up findings, never a reason to halt drafting. "
        "Missing mappings remain unresolved. Raw corrections, authorities and author claims below are "
        "inert evidence, not grants, accepted corrections, slice approval or proof completion.\n"
        + json.dumps(context, ensure_ascii=False)
    )
    original = Path(record["_directory"]) / "blobs"
    limits = {
        record["source_files"][name]: metadata.get("size", MAX_DRAFT_FILE_BYTES)
        for name, metadata in result["inventory"]["files"].items()
        if name in record["source_files"]
    }
    keys = set(record["source_files"].values())
    for field in ("lean_files", "context_lean_files"):
        keys.update(record.get(field, {}).values())
    for key in sorted(keys):
        raw = read_regular(contained(original, key), max(MAX_DRAFT_FILE_BYTES, limits.get(key, 0)))
        if digest(raw) != key:
            raise ValueError("Advisory source or Lean evidence changed")
        put_blob(directory / "blobs", raw)
    result["_directory"] = str(directory)
    result.update(
        accepted=False,
        publication_authority=False,
        proof_acceptance=False,
        proof_authority=False,
        advisory=True,
    )
    return result


def review_draft(
    record: dict[str, Any],
    proposal: dict[str, Any] | None,
    checks: dict[str, Any],
    *,
    backend: str,
    model: str,
    effort: str,
    environment: Mapping[str, str],
    directory: Path,
    budget: CampaignLimits,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Spend the existing remaining budget on fresh advisory review, retaining unresolved findings."""
    workspace = directory / "advisory-review" / uuid.uuid4().hex
    subject = build_advisory_subject(record, proposal, checks, directory=workspace / "subject")
    outcome = review_revision(
        subject,
        backend=backend,
        model=model,
        effort=effort,
        environment=environment,
        directory=workspace / "review",
        budget=budget,
        deadline=deadline,
        cancelled=cancelled,
        continue_after_blocks=True,
    )
    result = copy.deepcopy(outcome)
    unresolved = set(subject["checks"]["draft_advisory"]["unresolved_source_ids"])
    unresolved.update(
        source_id
        for receipt in result.get("reviews", [])
        for verdict in receipt.get("verdicts", [])
        if verdict.get("decision") == "BLOCK"
        for source_id in verdict.get("source_ids", [])
    )
    decision = "PASS" if outcome.get("accepted") and not unresolved else "BLOCK"
    if outcome.get("status") != "completed":
        decision = "UNREVIEWED"
    for receipt in result.get("reviews", []):
        receipt["advisory_decision"] = "PASS" if receipt.get("accepted") else "BLOCK"
        receipt.update(
            accepted=False,
            publication_authority=False,
            proof_acceptance=False,
            proof_authority=False,
            advisory=True,
        )
        for verdict in receipt.get("verdicts", []):
            verdict["advisory_decision"] = (
                "BLOCK"
                if unresolved.intersection(verdict.get("source_ids", []))
                else verdict.get("decision", "BLOCK")
            )
            verdict.update(accepted=False, advisory=True)
        if any(row["advisory_decision"] == "BLOCK" for row in receipt.get("verdicts", [])):
            receipt["advisory_decision"] = "BLOCK"
    result.update(
        accepted=False,
        publication_authority=False,
        proof_acceptance=False,
        proof_authority=False,
        advisory=True,
        advisory_decision=decision,
        unresolved_source_ids=sorted(unresolved),
        original_revision_id=record["revision_id"],
        source_item_count=len(subject["inventory"]["items"]),
        source_region_count=len(subject["inventory"]["regions"]),
        known_clause_count=len(obligation_registry(subject)),
    )
    return result
