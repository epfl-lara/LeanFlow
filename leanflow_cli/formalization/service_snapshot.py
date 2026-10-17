"""Project saved formalization evidence separately from exact currentness validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, digest
from leanflow_cli.formalization.inventory_authority import display_inventory
from leanflow_cli.formalization.records import (
    assert_source_current,
    load_receipt,
    source_artifact_id,
    source_bytes,
)
from leanflow_cli.formalization.service_artifacts import _artifacts, _display_preview
from leanflow_cli.formalization.service_corrections import (
    authority_options,
    item_corrections,
    mark_current_corrections,
    source_bundle_binding,
)
from leanflow_cli.formalization.service_draft_navigation import draft_item_mappings


def _ref(
    record: dict[str, Any],
    span: dict[str, Any],
    text: str = "",
    *,
    sources: dict[str, bytes] | None = None,
    literal_excerpt: bool = False,
) -> dict[str, Any]:
    result = {
        "artifact_id": source_artifact_id(record, span["file"]),
        "file": span["file"],
        "precision": span.get(
            "precision",
            span.get("mapping_precision", "approximate" if "page_index" in span else "exact"),
        ),
        "method": span.get(
            "mapping_method", "page_only" if "page_index" in span else "source_bytes"
        ),
        "text": span.get("text", text),
    }
    for key in (
        "line_start",
        "line_end",
        "byte_start",
        "byte_end",
        "page_index",
        "text_start",
        "text_end",
        "occurrence_id",
    ):
        if key in span:
            result[key] = span[key]
    if (
        "page_index" not in span
        and "line_start" not in span
        and span.get("coordinate_space") != "pdf_page"
    ):
        file = span["file"]
        if sources is None:
            raw = source_bytes(record, file)
        else:
            if file not in sources:
                sources[file] = source_bytes(record, file)
            raw = sources[file]
        result.update(
            line_start=raw[: span.get("byte_start", 0)].count(b"\n") + 1,
            line_end=raw[: max(span.get("byte_start", 0), span.get("byte_end", 1) - 1)].count(b"\n")
            + 1,
        )
    if (
        literal_excerpt
        and "page_index" not in span
        and span.get("coordinate_space") != "pdf_page"
        and type(span.get("byte_start")) is int
        and type(span.get("byte_end")) is int
    ):
        file = span["file"]
        if sources is None:
            raw = source_bytes(record, file)
        else:
            if file not in sources:
                sources[file] = source_bytes(record, file)
            raw = sources[file]
        result["text"] = raw[span["byte_start"] : span["byte_end"]].decode(
            "utf-8", errors="replace"
        )
    return result


def _verdicts(receipt: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """Retain every unit verdict so a later phase cannot erase an earlier rejection."""
    result: dict[str, list[dict[str, Any]]] = {}
    for review in (receipt or {}).get("reviews", []):
        for verdict in review.get("verdicts", []):
            if verdict.get("unit_id"):
                result.setdefault(verdict["unit_id"], []).append(verdict)
    return result


def _diagnostics(
    record: dict[str, Any], *, saved_readings_reviewed: bool = False
) -> list[dict[str, Any]]:
    """Expose exact command evidence and retained resolutions without generic blocker waivers."""
    from leanflow_cli.formalization.source_reconciliation import lexical_admission

    hard = {row["id"] for row in lexical_admission(record["inventory"])}
    return [
        {
            **diagnostic,
            "native_interpretation_required": bool(
                not saved_readings_reviewed
                and (
                    diagnostic.get("blocking")
                    and diagnostic["id"] not in hard
                    or diagnostic.get("pending_independent_review")
                )
            ),
            "native_interpretation_status": (
                ("saved_reviewed" if saved_readings_reviewed else "pending")
                if (
                    diagnostic.get("blocking")
                    and diagnostic["id"] not in hard
                    or diagnostic.get("pending_independent_review")
                )
                else None
            ),
            "source_refs": [
                _ref(record, span)
                for span in diagnostic.get(
                    "spans", [diagnostic["span"]] if "span" in diagnostic else []
                )
            ],
            "can_resolve_command": diagnostic["code"] == "unresolved_command",
            "can_resolve_conditional": diagnostic["code"] == "unresolved_conditional",
        }
        for diagnostic in record["inventory"]["diagnostics"]
    ]


def _saved_readings_reviewed(
    record: dict[str, Any], receipt: dict[str, Any] | None, inventory: dict[str, Any]
) -> bool:
    """Label exact saved whole-source reviews without checking present publication authority."""
    if (
        not receipt
        or receipt.get("status") != "published"
        or receipt.get("revision_id") != record["revision_id"]
        or record.get("workflow_protocol") != 2
        or record.get("proposal_phase") != "complete"
        or not record.get("source_reconciliation")
        or inventory is not record["inventory"]
        or not isinstance(receipt.get("reviews"), list)
    ):
        return False
    from leanflow_cli.formalization.source_reconciliation import assert_reconciliation_reviewed

    try:
        assert_reconciliation_reviewed(record, receipt["reviews"], historical=True)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return False
    return True


def _known_clauses(
    record: dict[str, Any],
    source: dict[str, Any],
    units: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
    sources: dict[str, bytes],
) -> list[dict[str, Any]]:
    """Expose protected clauses and saved decisions without inferring approval from a disposition."""
    return [
        {
            "id": row["id"],
            "description": row["description"],
            "reason": row["reason"],
            "source_refs": [_ref(record, span, sources=sources) for span in row["source_refs"]],
            "dispositions": [
                {**disposition, "unit_id": unit["id"]}
                for unit in units
                for disposition in unit.get("clause_dispositions", [])
                if disposition["obligation_id"] == row["id"]
            ],
            "verdicts": [
                clause
                for verdict in verdicts
                for clause in verdict.get("clause_verdicts", [])
                if clause["obligation_id"] == row["id"]
            ],
        }
        for row in source.get("known_obligations", [])
    ]


def snapshot(root: Path, record: dict[str, Any], *, verify_current: bool = True) -> dict[str, Any]:
    """Project exactly selected evidence without promoting historical success or model activity."""
    sources: dict[str, bytes] = {}
    receipt = load_receipt(record)
    receipt_status = (receipt or {}).get("status")
    receipt_error = (receipt or {}).get("error") or (receipt or {}).get("checks", {}).get("error")
    selection_conflict = record.get("_selection_conflict")
    proof_receipt = None
    proof_error = ""
    from leanflow_cli.formalization.freshness import (
        AcceptanceFreshness,
        published_acceptance_freshness,
    )

    freshness: AcceptanceFreshness = (
        published_acceptance_freshness(root, record, receipt, verify_bytes=False)
        if verify_current
        else {
            "current": False,
            "status": "historical",
            "reason": "Saved evidence only; check currentness before using a proof handoff",
        }
    )
    if verify_current and not selection_conflict:
        try:
            from leanflow_cli.formalization.proof_receipts import current_proof_receipt

            proof_receipt = current_proof_receipt(root, record, receipt, freshness=freshness)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            proof_error = str(error)
    inventory = display_inventory(record, reviewed=receipt_status == "published")
    verdicts = _verdicts(receipt)
    claims = {
        claim["source_id"]: claim
        for review in (receipt or {}).get("reviews", [])
        for claim in review.get("claims", [])
    }
    checks = (receipt or {}).get("checks", record.get("checks", {}))
    declarations = {row["name"]: row for row in checks.get("declarations", [])}
    if proof_receipt:
        declarations.update(proof_receipt["checks"]["profiles"])

    draft_preview, draft_preview_error = _display_preview(
        root, record, verify_current=verify_current
    )
    artifacts = _artifacts(record, proof_receipt, draft_preview)
    draft_mappings = draft_item_mappings(root, record, draft_preview)
    lean_artifacts = {
        artifact["path"]: artifact["id"]
        for artifact in artifacts
        if artifact["origin"] in {"lean", "proof", "context"}
    }
    correction_options = authority_options(record, inventory)
    all_references = [ref for unit in record["units"] for ref in unit["declarations"]]
    items = []
    for source in inventory["items"]:
        units = [unit for unit in record["units"] if source["id"] in unit["required_source_ids"]]
        references = [dict(ref) for unit in units for ref in unit["declarations"]]
        for ref in references:
            ref.setdefault("file", "")
            if ref["file"] in lean_artifacts:
                ref["artifact_id"] = lean_artifacts[ref["file"]]
            compiled = declarations.get(ref["name"], {})
            if ref["file"] and type(compiled.get("line_start")) is int:
                ref["line_start"] = compiled["line_start"]
        rows = [declarations[ref["name"]] for ref in references if ref["name"] in declarations]
        relevant = [
            verdict
            for unit in units
            for verdict in verdicts.get(unit["id"], [])
            if source["id"] in verdict.get("source_ids", [])
        ]
        blocked_review = any(verdict.get("decision") == "BLOCK" for verdict in relevant)
        passed_review = bool(units) and all(
            any(
                verdict.get("decision") == "PASS"
                and set(verdict.get("source_ids", [])) == set(unit["required_source_ids"])
                for verdict in verdicts.get(unit["id"], [])
            )
            for unit in units
        )
        proof = (
            "pending"
            if not rows or any(row.get("pending") for row in rows)
            else "conditional" if any(row.get("conditional") for row in rows) else "checked"
        )
        acceptance_stale = (
            verify_current and receipt_status == "published" and not freshness["current"]
        )
        if acceptance_stale:
            proof = "stale"
        items.append(
            {
                "id": source["id"],
                "title": source.get("title") or source.get("label") or source["kind"],
                "kind": source["kind"],
                "claim_status": claims.get(source["id"], {}).get(
                    "status", source.get("claim_status", "unclassified")
                ),
                "claim_status_rationale": claims.get(source["id"], {}).get("rationale", ""),
                "extracted_claim_status": source.get("claim_status", "unclassified"),
                "proof_authorization": source.get("proof_authorization"),
                "scope": source.get("scope", "candidate"),
                "source_refs": [
                    _ref(record, span, source.get("text", ""), sources=sources)
                    for span in source["statement_spans"]
                ]
                + [_ref(record, span, sources=sources) for span in source.get("pdf_refs", [])],
                "lean_refs": references,
                **(
                    {"draft_mapping": draft_mappings[source["id"]]}
                    if source["id"] in draft_mappings
                    else {}
                ),
                "known_obligations": _known_clauses(record, source, units, relevant, sources),
                "corrections": item_corrections(
                    record,
                    source["id"],
                    references=all_references,
                    checks=checks,
                    receipt=receipt,
                    artifacts=lean_artifacts,
                    source_ref=lambda span: _ref(
                        record, span, sources=sources, literal_excerpt=True
                    ),
                ),
                "correction_authority_options": correction_options[source["id"]],
                "review_status": (
                    "correction_needed"
                    if blocked_review
                    else (
                        "error"
                        if receipt_status == "review_error"
                        else "pass" if passed_review else "pending"
                    )
                ),
                "elaboration_status": (
                    "stale"
                    if acceptance_stale
                    else (
                        "error"
                        if receipt_status == "check_error"
                        else (
                            "rejected"
                            if receipt_status == "check_rejected"
                            else (
                                "checked"
                                if references and len(rows) == len(references)
                                else "pending"
                            )
                        )
                    )
                ),
                "proof_status": proof,
                "feedback": list(
                    dict.fromkeys(
                        str(value) for verdict in relevant for value in verdict.get("issues", [])
                    )
                ),
                "representation_notes": "\n\n".join(unit["representation_note"] for unit in units),
                "mapping_rationale": "\n\n".join(
                    dict.fromkeys(str(verdict.get("rationale", "")) for verdict in relevant)
                ),
            }
        )
    regions = [
        {
            "id": region["id"],
            "title": region.get(
                "title",
                f"Page {region['page_index'] + 1}" if "page_index" in region else region["file"],
            ),
            "classification": (
                "represented"
                if region["disposition"] in {"item", "represented_item"}
                else region["disposition"]
            ),
            "disposition": region["disposition"],
            "source_refs": [_ref(record, region, sources=sources)],
            "item_ids": region.get("item_ids", []),
            "reason": region.get("reason", ""),
            "repair_required": region.get("repair_required", False),
        }
        for region in inventory["regions"]
    ]
    stale = False
    if verify_current:
        try:
            assert_source_current(root, record)
        except (ValueError, OSError):
            stale = True
    status = (
        "selection_conflict"
        if selection_conflict
        else (receipt or {}).get("status", record.get("status", "inventory_pending"))
    )
    if verify_current and status == "published" and not freshness["current"]:
        status = freshness["status"]
        proof_error = freshness["reason"]
    handoff = (receipt or {}).get("handoff")
    lean_stale = bool(proof_error)
    publication_error = proof_error
    if verify_current and status == "published" and handoff:
        try:
            from leanflow_cli.formalization.publication import assert_no_pending_publication

            assert_no_pending_publication(root)
            for file, expected in (
                (proof_receipt or {}).get("after", handoff.get("files", {})).items()
            ):
                if digest(contained(root, file).read_bytes()) != expected:
                    lean_stale = True
        except (OSError, ValueError, RuntimeError) as error:
            lean_stale = True
            publication_error = str(error)
    unresolved = sum(
        region["classification"] == "unresolved" or region.get("repair_required", False)
        for region in regions
    )
    blocked = any(diagnostic.get("blocking") for diagnostic in inventory["diagnostics"])
    accepted_handoff = bool(
        verify_current
        and status == "published"
        and handoff
        and handoff.get("revision_id") == record["revision_id"]
        and not stale
        and not lean_stale
        and not blocked
        and not unresolved
        and not selection_conflict
        and not draft_preview_error
    )
    can_prove = bool(accepted_handoff and (handoff or {}).get("obligations") and not proof_receipt)
    mark_current_corrections(items, accepted_handoff=accepted_handoff)
    from leanflow_cli.formalization.service_history import revision_history

    history = revision_history(root, record["document_id"], record["revision_id"])
    revisions = history["revisions"]
    from leanflow_cli.formalization.preparation import preparation_status
    from leanflow_cli.formalization.slice_gate import slice_status

    return {
        "schema_version": 1,
        "currentness": "checked" if verify_current else "historical",
        "source_currentness": "stale" if stale else "checked" if verify_current else "unchecked",
        "draft_preview": draft_preview,
        "draft_preview_error": draft_preview_error or None,
        "document_id": record["document_id"],
        "revision_id": record["revision_id"],
        **({"source_bundle": record["source_bundle"]} if source_bundle_binding(record) else {}),
        "status": status,
        "receipt_error": str(receipt_error) if receipt_error else None,
        "acceptance_freshness": freshness,
        "preparation": preparation_status(root, verify_current=verify_current),
        "slice": slice_status(root, record),
        "source": {
            "kind": inventory["source_kind"],
            "path": inventory["source_relative"],
            "files": sorted(record["source_files"]),
            "pages": len(inventory.get("pages", [])),
        },
        "items": items,
        "regions": regions,
        "diagnostics": _diagnostics(
            {**record, "inventory": inventory},
            saved_readings_reviewed=_saved_readings_reviewed(record, receipt, inventory),
        )
        + ([draft_preview_error] if draft_preview_error else [])
        + (
            [
                publication_error
                or "Published Lean sources changed; proof handoff needs revalidation"
            ]
            if lean_stale
            else []
        )
        + (
            [str(selection_conflict.get("message", selection_conflict))]
            if selection_conflict
            else []
        ),
        "counts": {
            "included": sum(item["scope"] == "included" for item in items),
            "excluded": sum(item["scope"] == "excluded" for item in items),
            "candidates": sum(item["scope"] == "candidate" for item in items),
            "reviewed": sum(item["review_status"] == "pass" for item in items),
            "unresolved": unresolved,
            "source_files": len(record["source_files"]),
        },
        "layout": record.get("layout"),
        "artifacts": [
            {key: value for key, value in artifact.items() if key != "origin"}
            for artifact in artifacts
        ],
        "can_prove": can_prove,
        "source_stale": stale,
        "lean_stale": lean_stale,
        "handoff": handoff if accepted_handoff else None,
        "proof_revision_id": (proof_receipt or {}).get("proof_revision_id"),
        "proof_completion": (
            "checked"
            if proof_receipt or (accepted_handoff and not (handoff or {}).get("obligations"))
            else "pending"
        ),
        "selection_conflict": selection_conflict,
        "revisions": revisions,
        "revision_history": {key: history[key] for key in ("offset", "next_offset", "total")},
        "reviewer": (receipt or {}).get("reviewer"),
        "costs": (receipt or {}).get("costs"),
    }
