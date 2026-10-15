"""Expose complete frozen source evidence through a read-only reviewer capability."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.clause_obligations import obligation_registry, validate_dispositions
from leanflow_cli.formalization.environment import LIBRARY_SEMANTICS
from leanflow_cli.formalization.evidence import digest, identity, read_blob
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.records import source_bytes
from leanflow_cli.formalization.review_registry import compiler_base_policy, declaration_index
from leanflow_cli.formalization.review_scope import review_scope, scope_header
from leanflow_cli.formalization.review_transport import (
    REVIEW_RESPONSE_BYTES,
    _json_text,
    _response_size,
)
from leanflow_cli.formalization.review_transport import EvidenceBridge as EvidenceBridge
from leanflow_cli.formalization.source_reconciliation import (
    compact_requirements,
    reconciliation_requirements,
)
from leanflow_cli.formalization.source_review_regions import coalesce_regions, compact_region


def _span_text(record: dict[str, Any], span: dict[str, Any]) -> str | None:
    """Read a frozen span in its declared coordinate space, including companion pages."""
    relative = span["file"]
    if span.get("coordinate_space") == "pdf_page":
        pages = record["inventory"].get("pages", [])
        page = next(
            (
                page
                for page in pages
                if page["index"] == span["page_index"] and page.get("file", relative) == relative
            ),
            None,
        )
        if page is None:
            raise ValueError("Frozen PDF span has no matching physical page")
        text = str(page.get("text", ""))
        return text[span.get("text_start", 0) : span.get("text_end", len(text))]
    encoding = record["inventory"]["files"][relative].get("encoding", "utf-8")
    if encoding == "binary":
        return None
    return source_bytes(record, relative)[span["byte_start"] : span["byte_end"]].decode(encoding)


def evidence_documents(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep complete sources, regions, proofs, and compiled declaration evidence available."""
    documents: dict[str, dict[str, Any]] = {
        "inventory": record["inventory"],
        "full-inventory": complete_inventory(record),
        "controller-scope": review_scope(record),
        "layout": record["layout"],
        "controller-checks": record["checks"],
        "declarations": declaration_index(record["checks"]),
        "units": {"units": record["units"]},
    }
    for row in record["checks"]["declarations"]:
        documents["declaration:" + row["name"]] = row
    for row in record["checks"].get("library_declarations", []):
        documents["library-declaration:" + row["name"]] = {
            **row,
            "authority": (
                "independently-replayed-compiled-project-context"
                if row.get("role") == "project"
                else "independently-replayed-compiled-library"
            ),
            "source_build_correspondence": "not_claimed",
        }
    original = {relative: source_bytes(record, relative) for relative in record["source_files"]}
    for relative in record["source_files"]:
        raw = original[relative]
        if (
            record["inventory"]["source_kind"] == "pdf"
            or record["inventory"].get("companion_pdf", {}).get("file") == relative
        ):
            documents["source:" + relative] = {
                "sha256": digest(raw),
                "rendered_page_delivery": "unavailable",
                "physical_page_inventory": record["inventory"].get(
                    "page_inventory",
                    record["inventory"].get("companion_pdf", {}).get("page_inventory", {}),
                ),
                "review_requirement": (
                    "PDF-only mathematics requires a verified full-page transcription. "
                    "For a TeX companion, audit correspondence and additional content; "
                    "do not rely on unverified extraction for PDF-only mathematical evidence. "
                    "When rendered_page_delivery is read_source_image, retrieve all assigned "
                    "physical pages through that tool; extracted text alone is insufficient."
                ),
                "pages": [
                    page
                    for page in record["inventory"].get("pages", [])
                    if page.get("file", relative) == relative
                ],
            }
        else:
            encoding = record["inventory"]["files"][relative].get("encoding", "utf-8")
            documents["source:" + relative] = (
                {
                    "sha256": digest(raw),
                    "binary": True,
                    "review_requirement": "If mathematically relevant, require an explicitly verified transcription or block for visual source repair.",
                }
                if encoding == "binary"
                else {"sha256": digest(raw), "text": raw.decode(encoding)}
            )
    regions = {
        region["id"]: region
        for region in [*complete_inventory(record)["regions"], *record["inventory"]["regions"]]
    }
    for region in regions.values():
        relative = region.get("file")
        if relative not in original:
            continue
        text = _span_text(record, region)
        documents["region:" + region["id"]] = {
            "region": region,
            "original_text": text,
            "source_sha256": record["source_files"][relative],
            "binary_requires_visual_repair": text is None,
            **(
                {
                    "pdf_fidelity": next(
                        (
                            page.get("fidelity", {"status": "unverified"})
                            for page in record["inventory"].get("pages", [])
                            if page["index"] == region["page_index"]
                            and page.get("file", relative) == relative
                        ),
                        {"status": "unverified"},
                    ),
                    "rendered_page_delivery": "unavailable",
                }
                if region.get("coordinate_space") == "pdf_page"
                else {}
            ),
        }
    groups = coalesce_regions(record["inventory"]["regions"])
    documents["source-region-partition"] = {
        "canonical_region_count": len(record["inventory"]["regions"]),
        "membership_sha256": identity(groups),
        "groups": groups,
    }
    for group in groups:
        member = documents["region:" + group["member_region_ids"][0]]
        documents["region:" + group["id"]] = {
            **member,
            "region": compact_region(group),
            "original_text": _span_text(record, group),
        }
    stored_reading = record.get("source_reconciliation", {})
    requirements = reconciliation_requirements(
        {**record, "revision_id": stored_reading.get("base_revision_id", record["revision_id"])}
    )
    documents["source-reconciliation-requirements"] = requirements
    documents["source-reconciliation"] = {
        "requirements": requirements,
        "proposed_reading": record.get("source_reconciliation"),
        "approval": "none; exact source coverage and final coherence must independently pass",
    }
    obligations = obligation_registry(record)
    documents["known-obligations"] = {
        "discovery": "explicitly identified clauses only; completeness still requires source review",
        "obligations": list(obligations.values()),
    }
    for key, row in obligations.items():
        documents["obligation:" + key] = {
            **row,
            "source_text": [_span_text(record, ref) for ref in row["source_refs"]],
            "authority": "known source clause; no translation approval or partial-scope authorization",
        }
    lean_files = {**record.get("context_lean_files", {}), **record.get("lean_files", {})}
    for relative, key in lean_files.items():
        raw = read_blob(Path(record["_directory"]) / "blobs", key)
        documents["lean:" + relative] = {"sha256": key, "text": raw.decode()}
    return documents


def mandatory_dossier(
    record: dict[str, Any],
    units: list[dict[str, Any]],
    *,
    coherence: bool,
    documents: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Supply unabridged required items and proofs, with full frozen retrieval as backup."""
    items = {item["id"]: copy.deepcopy(item) for item in record["inventory"]["items"]}
    required = {key for unit in units for key in unit["required_source_ids"]}
    for unit in units:
        validate_dispositions(record, unit, compiled=True)
    for key in required:
        item = items[key]
        item["proof_text"] = [
            _span_text(record, span) for span in item.get("proof_spans", []) if "byte_start" in span
        ]
    if documents is None:
        documents = evidence_documents(record)
    referenced_files = {
        ref["file"] for unit in units for ref in unit["declarations"] if ref.get("file")
    }
    groups = coalesce_regions(record["inventory"]["regions"])
    return {
        "schema_version": 1,
        "phase": "coherence" if coherence else "translation",
        "controller_scope": scope_header(documents["controller-scope"]),
        "pdf_evidence_policy": {
            "rendered_page_delivery": (
                "read_source_image"
                if any(value.get("source_image_id") for value in documents.values())
                else "unavailable"
            ),
            "source_authority": record["inventory"]["source_kind"],
            "physical_page_inventory": record["inventory"].get(
                "page_inventory",
                record["inventory"].get("companion_pdf", {}).get("page_inventory", {}),
            ),
            "requirement": (
                "A trusted physical page census must cover the entire PDF. PDF-only pages "
                "require actor=user verified complete transcriptions, including "
                "mathematically relevant rasterized clauses and diagrams. Extracted text alone "
                "is insufficient even when nonempty. For TeX companions, check correspondence "
                "and extra mathematical content against EVERY assigned physical page using "
                "read_source_image. Actual image delivery is required for coverage/coherence PASS; "
                "text and image metadata alone are insufficient. The TeX remains claim authority; "
                "normal independent review certifies this correspondence assessment, without "
                "inventing user-verified transcriptions or PDF-only approval."
            ),
        },
        "units": units,
        "known_obligations": list(obligation_registry(record, required).values()),
        "clause_policy": (
            "Every known clause requires its authored disposition and independent clause verdict. "
            "Omitted, specialized and unresolved clauses block full-item PASS even when disclosed "
            "in representation_note. Only exact formal representations or named Prop proof slots "
            "with complete data/types may pass independent fidelity review. This registry does "
            "not claim automatic discovery of all mathematical clauses."
        ),
        "source_items": [items[key] for key in sorted(required)],
        "source_reconciliation": (
            compact_requirements(record["source_reconciliation"])
            if record.get("source_reconciliation")
            else None
        ),
        "coverage_partition": {
            "evidence_id": "source-region-partition",
            "membership_sha256": identity(groups),
            "canonical_region_count": len(record["inventory"]["regions"]),
            "group_count": len(groups),
        },
        "regions": (
            [
                {
                    **compact_region(region),
                    "evidence_id": "region:" + region["id"],
                    **(
                        {"source_image_id": documents["region:" + region["id"]]["source_image_id"]}
                        if documents.get("region:" + region["id"], {}).get("source_image_id")
                        else {}
                    ),
                    **(
                        {"pdf_fidelity": documents["region:" + region["id"]]["pdf_fidelity"]}
                        if region.get("coordinate_space") == "pdf_page"
                        else {}
                    ),
                    "text_delivery": (
                        "visual_repair"
                        if documents.get("region:" + region["id"], {}).get("original_text") is None
                        else (
                            "paged"
                            if _response_size(documents["region:" + region["id"]])
                            > REVIEW_RESPONSE_BYTES
                            else "inline"
                        )
                    ),
                    "original_text": (
                        documents.get("region:" + region["id"], {}).get("original_text")
                        if _response_size(documents.get("region:" + region["id"], {}))
                        <= REVIEW_RESPONSE_BYTES
                        else None
                    ),
                }
                for region in groups
            ]
            if coherence
            else []
        ),
        "excluded_items": [item for item in items.values() if item.get("scope") == "excluded"],
        "setup_notes": record.get("setup_notes", ""),
        "layout": record["layout"],
        "compiled_declarations": [
            {
                **{key: value for key, value in row.items() if key != "body"},
                "evidence_id": "declaration:" + row["name"],
                "body_delivery": "read_evidence",
            }
            for row in record["checks"]["declarations"]
            if row["name"] in {ref["name"] for unit in units for ref in unit["declarations"]}
        ],
        "library_evidence": {
            "authority": "Exact kernel-checked compiled declarations, not presumed source/build correspondence",
            "semantics": copy.deepcopy(LIBRARY_SEMANTICS),
            "review_requirement": (
                "Check the mathematical meaning of every referenced library or existing project "
                "definition and type through library-declaration evidence; names, Git pins and "
                "source files alone do not establish it. Project-role entries expose actual compiled "
                "local context, not approval inferred from its source text. "
                "The listed semantic surface follows statement types and data definitions, excluding "
                "proof-only dependencies. The complete audit closure remains in the evidence catalog. "
                "Block when the compiled meaning cannot be assessed; do not substitute trust in names."
            ),
            "audit_declaration_count": len(record["checks"].get("library_declarations", [])),
            "declarations": [
                {
                    "name": row["name"],
                    "module": row["module"],
                    "role": row.get("role", "dependency"),
                    "evidence_id": "library-declaration:" + row["name"],
                }
                for row in record["checks"].get("library_declarations", [])
                if row.get("semantic_surface", True)
            ],
        },
        "compiler_base_policy": compiler_base_policy(record["checks"]),
        "review_reading_order": (
            "Read this complete dossier, full source/context and required region coverage, "
            "then every mapped exact declaration row and referenced noncompiler semantic "
            "definition/type row. Follow direct_dependencies when mathematical meaning "
            "requires it. declarations is a complete registry index with exact row IDs. "
            "controller-checks retains the unabridged compilation audit, including duplicate "
            "profiles/obligations and kernel import paths; retrieve it when provenance or "
            "audit details are needed. Audit inventories never replace mathematical evidence."
        ),
        "lean_files": {name: documents["lean:" + name] for name in sorted(referenced_files)},
        "evidence_catalog": {
            key: {
                "sha256": identity(value),
                "characters": len(json.dumps(value, ensure_ascii=False)),
                "utf8_bytes": len(_json_text(value).encode("utf-8")),
            }
            for key, value in documents.items()
        },
        "source_bundle": record["source_bundle"],
        "environment_id": record["environment_id"],
        "policy_id": record["policy_id"],
    }


def review_assignment(
    record: dict[str, Any],
    dossier: dict[str, Any],
    dossier_id: str,
    *,
    documents: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Page large assignments instead of silently truncating them or overflowing native prompts."""
    # The catalog belongs to one frozen checking phase; each bridge owns its additions.
    documents = dict(evidence_documents(record) if documents is None else documents)
    header = scope_header(documents["controller-scope"])
    if "controller_scope" in dossier and dossier["controller_scope"] != header:
        raise ValueError("Reviewer assignment differs from frozen controller scope")
    assignment = {
        "revision_id": record["revision_id"],
        "dossier_id": dossier_id,
        "controller_scope": header,
        "dossier": dossier,
    }
    if _response_size(assignment) <= REVIEW_RESPONSE_BYTES:
        return assignment, documents
    documents["assignment-dossier"] = dossier
    return {
        "revision_id": record["revision_id"],
        "dossier_id": dossier_id,
        "controller_scope": header,
        "dossier_ref": "assignment-dossier",
        "required_read": "Read every page of assignment-dossier through read_evidence before reviewing; no content was omitted. Its evidence catalog provides full surrounding sources.",
        "unit_count": len(dossier["units"]),
        "region_count": len(dossier["regions"]),
        "characters": len(_json_text(dossier)),
        "utf8_bytes": len(_json_text(dossier).encode("utf-8")),
        "sha256": identity(dossier),
    }, documents
