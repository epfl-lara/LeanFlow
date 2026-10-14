"""Expose revision-bound formalization snapshots, repairs, and frozen editor artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization.actions import apply_action
from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    durable_json,
    identifier,
    identity,
    read_blob,
)
from leanflow_cli.formalization.records import (
    DocumentSelectionConflict,
    assert_source_current,
    create_document,
    document_directory,
    document_lock,
    load_document,
    load_receipt,
    new_revision,
    rehydrate_document,
    save_revision,
    selection_pointer,
    source_artifact_id,
    source_bytes,
)


def _artifacts(
    record: dict[str, Any], proof_receipt: dict[str, Any] | None = None
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
    return result


def _ref(record: dict[str, Any], span: dict[str, Any], text: str = "") -> dict[str, Any]:
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
        raw = source_bytes(record, span["file"])
        result.update(
            line_start=raw[: span.get("byte_start", 0)].count(b"\n") + 1,
            line_end=raw[: max(span.get("byte_start", 0), span.get("byte_end", 1) - 1)].count(b"\n")
            + 1,
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


def _diagnostics(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose exact command evidence and retained resolutions without generic blocker waivers."""
    return [
        {
            **diagnostic,
            "source_refs": [
                _ref(record, span)
                for span in diagnostic.get(
                    "spans", [diagnostic["span"]] if "span" in diagnostic else []
                )
            ],
            "can_resolve_command": diagnostic["code"] == "unresolved_command",
        }
        for diagnostic in record["inventory"]["diagnostics"]
    ]


def snapshot(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Project exactly selected evidence without promoting historical success or model activity."""
    receipt = load_receipt(record)
    receipt_status = (receipt or {}).get("status")
    receipt_error = (receipt or {}).get("error") or (receipt or {}).get("checks", {}).get("error")
    selection_conflict = record.get("_selection_conflict")
    proof_receipt = None
    proof_error = ""
    if not selection_conflict:
        try:
            from leanflow_cli.formalization.proof_receipts import current_proof_receipt

            proof_receipt = current_proof_receipt(root, record, receipt)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            proof_error = str(error)
    inventory = record["inventory"]
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
    artifacts = _artifacts(record, proof_receipt)
    lean_artifacts = {
        artifact["path"]: artifact["id"]
        for artifact in artifacts
        if artifact["origin"] in {"lean", "proof"}
    }
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
                    _ref(record, span, source.get("text", "")) for span in source["statement_spans"]
                ]
                + [_ref(record, span) for span in source.get("pdf_refs", [])],
                "lean_refs": references,
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
                    "error"
                    if receipt_status == "check_error"
                    else (
                        "rejected"
                        if receipt_status == "check_rejected"
                        else "checked" if references and len(rows) == len(references) else "pending"
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
            "source_refs": [_ref(record, region)],
            "item_ids": region.get("item_ids", []),
            "reason": region.get("reason", ""),
            "repair_required": region.get("repair_required", False),
        }
        for region in inventory["regions"]
    ]
    stale = False
    try:
        assert_source_current(root, record)
    except (ValueError, OSError):
        stale = True
    status = (
        "selection_conflict"
        if selection_conflict
        else (receipt or {}).get("status", record.get("status", "inventory_pending"))
    )
    handoff = (receipt or {}).get("handoff")
    lean_stale = bool(proof_error)
    publication_error = proof_error
    if status == "published" and handoff:
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
        status == "published"
        and handoff
        and handoff.get("revision_id") == record["revision_id"]
        and not stale
        and not lean_stale
        and not blocked
        and not unresolved
        and not selection_conflict
    )
    can_prove = bool(accepted_handoff and (handoff or {}).get("obligations") and not proof_receipt)
    revisions = []
    seen = set()
    for directory in (
        document_directory(root, record["document_id"]),
        document_directory(root, record["document_id"], published=True),
    ):
        parent = contained(directory, "revisions")
        if parent.exists():
            for path in sorted(parent.glob("*.json")):
                if path.stem not in seen:
                    historic = load_document(root, record["document_id"], path.stem)
                    historic_receipt = load_receipt(historic)
                    revisions.append(
                        {
                            "revision_id": historic["revision_id"],
                            "status": (historic_receipt or {}).get(
                                "status", historic.get("status", "inventory_pending")
                            ),
                        }
                    )
                    seen.add(path.stem)
    return {
        "schema_version": 1,
        "document_id": record["document_id"],
        "revision_id": record["revision_id"],
        "status": status,
        "receipt_error": str(receipt_error) if receipt_error else None,
        "source": {
            "kind": inventory["source_kind"],
            "path": inventory["source_relative"],
            "files": sorted(record["source_files"]),
            "pages": len(inventory.get("pages", [])),
        },
        "items": items,
        "regions": regions,
        "diagnostics": _diagnostics(record)
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
        "reviewer": (receipt or {}).get("reviewer"),
        "costs": (receipt or {}).get("costs"),
    }


def inspect_document(
    root: Path, source: Path, *, companion_pdf: Path | None = None
) -> dict[str, Any]:
    """Freeze intake and return the source audit without launching a model or creating Lean files."""
    root = root.resolve()
    selected = source if source.is_absolute() else root / source
    if selected.is_dir():
        from leanflow_cli.formalization.formalization_documents import (
            resolve_formalization_document,
        )

        selected, _, _ = resolve_formalization_document(root, root, str(selected))
    return snapshot(root, create_document(root, selected, companion_pdf=companion_pdf))


def show_document(root: Path, document_id: str) -> dict[str, Any]:
    """Read the selected private revision, falling back to portable published evidence."""
    try:
        record = load_document(root, identifier(document_id))
    except DocumentSelectionConflict as error:
        record = load_document(root, identifier(document_id), error.private_revision)
        record["_selection_conflict"] = {
            "message": str(error),
            "private_revision_id": error.private_revision,
            "published_revision_id": error.published_revision,
        }
    return snapshot(root.resolve(), record)


def list_documents(root: Path) -> dict[str, Any]:
    """List controller-owned documents through the public snapshot protocol."""
    ids: set[str] = set()
    for prefix in (".leanflow/formalizations", "formalization"):
        directory = contained(root.resolve(), prefix)
        if directory.exists():
            for path in directory.iterdir():
                if path.is_dir() and not path.is_symlink() and (path / "current.json").is_file():
                    ids.add(identifier(path.name))
    return {"schema_version": 1, "documents": [show_document(root, key) for key in sorted(ids)]}


def act_document(
    root: Path, document_id: str, expected_revision: str, action: dict[str, Any]
) -> dict[str, Any]:
    """Serialize a typed edit against the exact visible revision and invalidate prior acceptance."""
    root = root.resolve()
    if not isinstance(action, dict):
        raise ValueError("Formalization action must be a JSON object")
    from leanflow_cli.formalization.publication import publication_guard

    with publication_guard(root), document_lock(root, document_id) as directory:
        try:
            record = load_document(root, document_id)
        except DocumentSelectionConflict as error:
            if action.get("type") != "select_revision":
                raise
            record = load_document(root, document_id, error.private_revision)
        if record["revision_id"] != identifier(expected_revision):
            raise ValueError(
                "Formalization revision changed; refresh the inventory before applying this action"
            )
        rehydrate_document(record, directory)
        if action.get("type") == "select_revision":
            selected = load_document(
                root, document_id, identifier(str(action.get("revision_id", "")))
            )
            rehydrate_document(selected, directory)
            durable_json(
                contained(directory, "current.json"),
                selection_pointer(directory, selected["revision_id"], revoke_authority=True),
            )
            selected = load_document(root, document_id)
        else:
            selected = new_revision(record)
            apply_action(selected, action)
            selected = save_revision(directory, selected)
        return snapshot(root, selected)


def resolve_artifact(
    root: Path, document_id: str, expected_revision: str, artifact_id: str
) -> dict[str, Any]:
    """Materialize an allowlisted immutable artifact on the host owning the workspace."""
    from leanflow_cli.formalization.proof_receipts import current_proof_receipt
    from leanflow_cli.formalization.publication import publication_guard

    with publication_guard(root), document_lock(root, document_id) as directory:
        record = load_document(root, document_id)
        if record["revision_id"] != identifier(expected_revision):
            raise ValueError("Formalization revision changed; refresh before opening its source")
        proof_receipt = current_proof_receipt(root, record, load_receipt(record))
        candidates = [
            artifact
            for artifact in _artifacts(record, proof_receipt)
            if artifact["id"] == artifact_id
        ]
        if len(candidates) != 1:
            raise ValueError("Artifact does not belong to this selected revision")
        artifact = candidates[0]
        data = (
            contained(root, artifact["path"]).read_bytes()
            if artifact["origin"] == "proof"
            else read_blob(contained(Path(record["_directory"]), "blobs"), artifact["sha256"])
        )
        if digest(data) != artifact["sha256"]:
            raise ValueError("Certified proof source changed before opening")
        suffix = Path(artifact["path"]).suffix
        target = contained(directory, f"viewer/{identifier(artifact_id)}{suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or digest(target.read_bytes()) != artifact["sha256"]:
                raise ValueError("Frozen viewer artifact was modified")
        else:
            with target.open("xb") as handle:
                handle.write(data)
            target.chmod(0o444)
        return {
            key: value
            for key, value in {**artifact, "path": str(target)}.items()
            if key != "origin"
        }
