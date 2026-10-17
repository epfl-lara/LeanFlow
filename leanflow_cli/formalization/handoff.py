"""Bind reviewed drafts to exact prover obligations and immutable proof-only receipts."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.corrections import assert_corrections_accepted
from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import contained, digest, durable_json, identity
from leanflow_cli.formalization.handoff_slots import compiled_entry, source_entries
from leanflow_cli.formalization.lean_inspector import INSPECTOR
from leanflow_cli.formalization.lean_policy import POLICY_VERSION, inspect_draft
from leanflow_cli.formalization.proof_snapshot import check_active, inspect_frozen_proof
from leanflow_cli.formalization.records import (
    assert_source_current,
    document_directory,
    document_lock,
    load_document,
    load_receipt,
)
from leanflow_cli.workflows.prover.source import (
    SourceDocument,
    project_path,
    read_source,
)


def _protected_registry(registry: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Keep exact profiles and distinguish owned proof slots from read-only source reuse."""
    return {
        row["name"]: {
            **{key: row[key] for key in ("module", "kind", "type_hash", "body_hash")},
            "file": row.get("file", ""),
            "declaration_origin": row.get(
                "declaration_origin", "generated" if row.get("file") else "dependency"
            ),
        }
        for row in registry["declarations"]
    }


def build_handoff(
    root: Path,
    *,
    document_id: str,
    revision_id: str,
    registry: dict[str, Any],
    files: dict[str, str],
    environment_id: str,
    guidance: str = "",
    source_ids: dict[str, list[str]] | None = None,
    allowed_axioms: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound"),
    guidance_refs: list[dict[str, Any]] | None = None,
    definition_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project compiled pending declarations into exact original source proof slots."""
    if registry.get("accepted") is not True:
        raise ValueError("A handoff requires an accepted compiled registry")
    obligations = []
    entries: dict[str, list[dict[str, Any]]] = {}
    for row in registry["obligations"]:
        if row.get("declaration_origin", "generated") != "generated":
            raise ValueError("Read-only reused declarations cannot authorize proof writes")
        relative = row["file"]
        source = read_source(project_path(root, relative))
        if files.get(relative) != digest(source.encode("utf-8")):
            raise ValueError(f"Handoff source differs from the accepted revision: {relative}")
        if relative not in entries:
            entries[relative] = source_entries(source)
        entry = compiled_entry(source, row, entries[relative])
        obligations.append(
            {
                "id": "proof-" + identity([revision_id, relative, row["name"], entry["holes"]]),
                "file": relative,
                "module": row["module"],
                "declaration": row["name"],
                "source_name": entry["source_name"],
                "reviewed_type_hash": row["type_hash"],
                "holes": entry["holes"],
                "hole_spans": entry["hole_spans"],
                **(
                    {"declaration_span": entry["declaration_span"]}
                    if "declaration_span" in entry
                    else {}
                ),
                "source_ids": (source_ids or {}).get(row["name"], []),
                "depends_on": row["depends_on"],
            }
        )
    result = {
        "schema_version": 1,
        "document_id": document_id,
        "revision_id": revision_id,
        "environment_id": environment_id,
        "acceptance_policy": {
            "policy_id": POLICY_VERSION,
            "checker_sha256": digest(INSPECTOR),
            "allowed_axioms": sorted(set(allowed_axioms) - {"sorryAx"}),
        },
        "files": files,
        "modules": registry.get("modules", {}),
        "protected_declarations": _protected_registry(registry),
        "hole_offset_unit": "unicode_codepoint",
        "obligations": obligations,
        "guidance": guidance,
        "guidance_refs": guidance_refs or [],
        "definition_refs": definition_refs or [],
    }
    if registry.get("correction_evidence"):
        result["source_corrections"] = copy.deepcopy(registry["correction_evidence"])
    return {**result, "handoff_id": "handoff-" + identity(result)}


def authorized_holes(handoff: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return only the source files and exact declarations authorized for proving."""
    result: dict[str, list[dict[str, Any]]] = {}
    for item in handoff["obligations"]:
        result.setdefault(item["file"], []).append(item)
    return result


def validate_handoff(
    root: Path,
    path: Path,
    revision_id: str,
    *,
    check_sources: bool = True,
    checked_environment_id: str | None = None,
) -> dict[str, Any]:
    """Authenticate evidence, reusing a caller's already checked environment only when exact."""
    from leanflow_cli.formalization.publication import assert_no_pending_publication

    assert_no_pending_publication(root)
    from leanflow_cli.formalization.freshness import assert_revision_not_revoked

    assert_revision_not_revoked(root, revision_id)
    path = contained(root, path.relative_to(root).as_posix())
    from leanflow_cli.formalization.publication_case_paths import assert_exact_control_path

    assert_exact_control_path(root, path.relative_to(root).as_posix())
    handoff = json.loads(path.read_text(encoding="utf-8"))
    if handoff.get("schema_version") != 1 or handoff.get("revision_id") != revision_id:
        raise ValueError("Formalization handoff revision does not match the requested revision")
    if handoff.get("handoff_id") != "handoff-" + identity(
        {key: value for key, value in handoff.items() if key != "handoff_id"}
    ):
        raise ValueError("Formalization handoff bytes differ from their identity")
    directory = document_directory(root, handoff["document_id"], published=True)
    if path != directory / "handoff.json":
        raise ValueError("Formalization handoff must be the portable published projection")
    assert_exact_control_path(root, f"formalization/{handoff['document_id']}/current.json")
    selected = json.loads((directory / "current.json").read_text(encoding="utf-8"))
    if selected.get("revision_id") != revision_id:
        raise ValueError("Formalization handoff is not the selected committed revision")
    if load_document(root, handoff["document_id"])["revision_id"] != revision_id:
        raise ValueError("Formalization handoff is no longer the selected document revision")
    record = load_document(root, handoff["document_id"], revision_id)
    from leanflow_cli.formalization.inventory_authority import assert_complete_coverage

    assert_complete_coverage(record)
    assert_source_current(root, record)
    registry = record["checks"]
    policy = {
        "policy_id": POLICY_VERSION,
        "checker_sha256": digest(INSPECTOR),
        "allowed_axioms": sorted(set(record["allowed_axioms"]) - {"sorryAx"}),
    }
    if handoff.get("acceptance_policy") != policy or (
        registry.get("declarations")
        and any(registry.get(key) != value for key, value in policy.items())
    ):
        raise ValueError("Formalization reviewed acceptance policy or checker changed")
    protected = _protected_registry(registry)
    expected_files = {
        key: value
        for key, value in record["project_source_hashes"].items()
        if key.endswith(".lean")
    }
    if (
        handoff["environment_id"] != record["environment_id"]
        or handoff["files"] != expected_files
        or handoff["modules"] != registry["modules"]
        or handoff["protected_declarations"] != protected
        or handoff.get("source_corrections", []) != registry.get("correction_evidence", [])
    ):
        raise ValueError("Formalization handoff differs from immutable compiled acceptance")
    pending = {row["name"]: row for row in registry["obligations"]}
    if {item["declaration"] for item in handoff["obligations"]} != set(pending) or len(
        handoff["obligations"]
    ) != len(pending):
        raise ValueError("Formalization handoff does not contain the exact accepted obligations")
    for item in handoff["obligations"]:
        row = pending[item["declaration"]]
        if (
            any(item[key] != row[key] for key in ("file", "module", "depends_on"))
            or item["reviewed_type_hash"] != row["type_hash"]
        ):
            raise ValueError("Formalization obligation differs from immutable compiled acceptance")
    receipt = load_receipt({**record, "_directory": str(directory)})
    selected_receipt = load_receipt(record)
    if (
        receipt is None
        or receipt.get("status") != "published"
        or receipt.get("revision_id") != revision_id
        or receipt.get("handoff") != handoff
        or selected_receipt != receipt
    ):
        raise ValueError("Formalization handoff is not bound to a published acceptance receipt")
    assert_corrections_accepted(record, receipt.get("reviews", []))
    for reference in handoff.get("guidance_refs", []):
        if digest(contained(root, reference["artifact"]).read_bytes()) != reference["sha256"]:
            raise ValueError("Formalization proof guidance artifact changed")
    current_environment = (
        environment_identity(root) if checked_environment_id is None else checked_environment_id
    )
    if current_environment != handoff["environment_id"]:
        raise ValueError("Formalization handoff compiler/dependency environment changed")
    if check_sources:
        for relative, expected in handoff["files"].items():
            if digest(contained(root, relative).read_bytes()) != expected:
                raise ValueError(
                    f"Formalization handoff source changed: {relative}; "
                    "resume the original scoped prover run or re-review before starting a new run"
                )
    from leanflow_cli.formalization.publication_membership import require_committed_publication

    require_committed_publication(root, handoff["document_id"], revision_id)
    return handoff


def proof_preservation_certificate(
    root: Path,
    handoff: dict[str, Any],
    documents: dict[str, SourceDocument],
    workspace: Path,
    allowed_axioms: tuple[str, ...],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Recheck proof-only changes and preserve source review without mutating its receipt."""
    from leanflow_cli.formalization.publication import publication_guard

    with publication_guard(root), document_lock(root, handoff["document_id"]):
        return _proof_preservation_locked(
            root,
            handoff,
            documents,
            workspace,
            allowed_axioms,
            deadline=deadline,
            cancelled=cancelled,
        )


def _proof_preservation_locked(
    root: Path,
    handoff: dict[str, Any],
    documents: dict[str, SourceDocument],
    workspace: Path,
    allowed_axioms: tuple[str, ...],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    """Hold project and document ownership across proof evidence and publication."""
    check_active(deadline, cancelled)
    saved_axioms = tuple(handoff["acceptance_policy"]["allowed_axioms"])
    if set(allowed_axioms) != set(saved_axioms):
        raise ValueError("Prover axiom policy differs from the reviewed acceptance policy")
    directory = document_directory(root, handoff["document_id"], published=True)
    if (
        validate_handoff(
            root, directory / "handoff.json", handoff["revision_id"], check_sources=False
        )
        != handoff
    ):
        raise ValueError("Formalization handoff changed during proving")
    if environment_identity(root) != handoff["environment_id"]:
        raise ValueError("Formalization environment changed during proving")
    allowed = authorized_holes(handoff)
    after: dict[str, str] = {}
    for relative, before_hash in handoff["files"].items():
        path = contained(root, relative)
        document = documents.get(relative)
        if document is None:
            if relative in allowed or digest(path.read_bytes()) != before_hash:
                raise ValueError(f"Untargeted accepted source changed during proving: {relative}")
            after[relative] = before_hash
            continue
        if digest(document.baseline.encode()) != before_hash:
            raise ValueError(f"Proof baseline differs from accepted source: {relative}")
        permitted = {index for item in allowed.get(relative, []) for index in item["holes"]}
        if (
            document.imports
            or document.proof_imports
            or {int(index) for index in document.replacements} - permitted
        ):
            raise ValueError(
                "Source review inheritance allows only the designated proof-slot replacements"
            )
        document.assert_current(root)
        after[relative] = digest(path.read_bytes())
    result = inspect_frozen_proof(
        root,
        list(handoff["modules"].values()),
        after,
        handoff["environment_id"],
        saved_axioms,
        inspect=inspect_draft,
        environment=environment_identity,
        deadline=deadline,
        cancelled=cancelled,
    )
    if result.get("accepted") is not True:
        raise ValueError(f"Proof revision failed compiled policy: {result.get('error')}")
    profiles = result["profiles"]
    for name, before in handoff["protected_declarations"].items():
        if before["declaration_origin"] != "generated":
            # Reuse has no proof-write authority. The exact environment and all
            # frozen project source hashes protect these read-only declarations.
            continue
        current = profiles.get(name)
        if not current or current["type_hash"] != before["type_hash"]:
            raise ValueError(f"Reviewed declaration type changed: {name}")
        if before["kind"] != "theorem" and current["body_hash"] != before["body_hash"]:
            raise ValueError(f"Reviewed definition body changed: {name}")
    for item in handoff["obligations"]:
        if "sorryAx" in profiles[item["declaration"]]["axioms"]:
            raise ValueError(
                f"Formalization proof obligation remains conditional: {item['declaration']}"
            )
    certificate = {
        "schema_version": 1,
        "kind": "proof_preservation",
        "reviewed_revision_id": handoff["revision_id"],
        "handoff_id": handoff["handoff_id"],
        "environment_id": handoff["environment_id"],
        "acceptance_policy": handoff["acceptance_policy"],
        "before": handoff["files"],
        "after": after,
        "checks": result,
    }
    certificate["proof_revision_id"] = "proof-rev-" + identity(certificate)
    check_active(deadline, cancelled)
    if any(
        digest(contained(root, relative).read_bytes()) != value for relative, value in after.items()
    ):
        raise ValueError("Proof source changed before certificate publication")
    if (
        validate_handoff(
            root, directory / "handoff.json", handoff["revision_id"], check_sources=False
        )
        != handoff
    ):
        raise ValueError("Formalization handoff changed before certificate publication")
    check_active(deadline, cancelled)
    target = directory / "proof-revisions" / f"{certificate['proof_revision_id']}.json"
    if target.exists():
        if json.loads(target.read_text()) != certificate:
            raise ValueError("An immutable proof-preservation certificate was changed")
    else:
        durable_json(target, certificate)
    return certificate
