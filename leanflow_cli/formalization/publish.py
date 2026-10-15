"""Publish one reviewed revision and its portable evidence as a recoverable write set."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import contained, digest, durable_json, read_blob
from leanflow_cli.formalization.handoff import build_handoff
from leanflow_cli.formalization.records import (
    assert_source_current,
    document_lock,
    load_document,
    load_receipt,
    receipt_events,
    revision_content,
    save_receipt,
)
from leanflow_cli.workflows.prover.lean_environment import lean_environment_generation


def portable_baseline(root: Path, document_id: str) -> dict[str, bytes]:
    """Capture published metadata at campaign start, before any author/reviewer runs."""
    from leanflow_cli.formalization.publication_case_paths import exact_control_path

    if not exact_control_path(root, f"formalization/{document_id}"):
        return {}
    directory = contained(root, f"formalization/{document_id}")
    if not directory.exists():
        return {}
    result = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Portable formalization evidence cannot contain symlinks")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def blueprint(record: dict[str, Any]) -> str:
    """Render navigable mappings from the accepted data rather than author status claims."""
    lines = [
        "# Formalization blueprint",
        "",
        f"Document `{record['document_id']}` · revision `{record['revision_id']}`",
        "",
        "Source fidelity, Lean elaboration, and proof completion are separate gates.",
        "",
        record.get("setup_notes", ""),
        "",
        "| Source items | Lean declarations | Representation |",
        "| --- | --- | --- |",
    ]
    items = {item["id"]: item for item in record["inventory"]["items"]}
    for unit in record["units"]:
        refs = ", ".join(
            (
                f"[{ref['name']}](../../{quote(ref['file'])})"
                if ref.get("file")
                else f"`{ref['name']}` (pinned library)"
            )
            for ref in unit["declarations"]
        )
        sources = []
        for item_id in unit["required_source_ids"]:
            item = items[item_id]
            spans = item.get("statement_spans", [])
            if spans:
                span = spans[0]
                fragment = f"#page={span['page_index'] + 1}" if "page_index" in span else ""
                sources.append(
                    f"[{item_id}](blobs/{record['source_files'][span['file']]}{fragment})"
                )
            else:
                sources.append(item_id)
        lines.append(
            f"| {', '.join(sources) or 'Supporting helper'} | {refs} | {unit['representation_note'].replace('|', '/').replace(chr(10), ' ')} |"
        )
    pending = record["checks"].get("obligations", [])
    lines.extend(["", f"Pending named proof obligations: {len(pending)}.", ""])
    lines.extend(f"- `{row['name']}` in `{row['file']}`" for row in pending)
    lines.extend(
        [
            "",
            "The immutable source files, region inventory, exact mapping and review verdicts are stored with this blueprint. Use the editor inventory to navigate source/PDF and Lean references.",
            "",
        ]
    )
    return "\n".join(lines)


def publish_checked(
    root: Path,
    baseline: dict[str, bytes],
    metadata_baseline: dict[str, bytes],
    checked: dict[str, Any],
    review: dict[str, Any],
    directory: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Keep acceptance staged on unsupported hosts; publish only the exact reviewed selection."""
    from leanflow_cli.formalization.publication import (
        PublicationUnsupported,
        freeze_publication_plan,
        publication_guard,
        publish_revision,
    )

    if not checked.get("accepted") or not review.get("accepted"):
        raise ValueError("Both compiled policy and independent source review must pass")
    record = checked["record"]
    from leanflow_cli.formalization.slice_gate import assert_complete_slice

    assert_complete_slice(root, record)
    revision, document_id = record["revision_id"], record["document_id"]
    from leanflow_cli.formalization.records import current_selection_token

    def assert_selection_authority() -> None:
        if (
            not checked.get("selection_token")
            or current_selection_token(root, document_id) != checked["selection_token"]
        ):
            raise ValueError(
                "Explicit source selection changed; restart formalization from the selected revision"
            )

    assert_selection_authority()
    store = Path(record["_directory"])
    source_ids: dict[str, list[str]] = {}
    for unit in record["units"]:
        for reference in unit["declarations"]:
            source_ids.setdefault(reference["name"], []).extend(unit["required_source_ids"])
    guidance = blueprint(record)
    guidance_refs = [
        {
            "source_id": item["id"],
            "artifact": f"formalization/{document_id}/blobs/{record['source_files'][span['file']]}",
            "sha256": record["source_files"][span["file"]],
            "role": role,
            "span": span,
        }
        for item in record["inventory"]["items"]
        for role in ("statement", "proof", "context")
        for span in item.get(role + "_spans", [])
    ]
    proof_guidance = (
        guidance
        + "\nRead the frozen source statement and original proof artifacts in the handoff's guidance_refs before proving; the source review permits proof changes only, preserving definitions, types and imports.\n"
    )
    handoff = build_handoff(
        Path(checked["frozen_root"]),
        document_id=document_id,
        revision_id=revision,
        registry=record["checks"],
        files={
            key: value
            for key, value in record["project_source_hashes"].items()
            if key.endswith(".lean")
        },
        environment_id=record["environment_id"],
        allowed_axioms=tuple(record["allowed_axioms"]),
        guidance=proof_guidance,
        guidance_refs=guidance_refs,
        definition_refs=[
            {key: row.get(key, "") for key in ("name", "module", "file", "type_hash", "body_hash")}
            for row in record["checks"]["declarations"]
            if row["kind"] != "theorem"
        ],
        source_ids=source_ids,
    )
    receipt = {
        "schema_version": 1,
        "revision_id": revision,
        "document_id": document_id,
        "status": "accepted_staged",
        "checks": record["checks"],
        "reviews": review["reviews"],
        "handoff": handoff,
        "environment_id": record["environment_id"],
    }
    receipt = save_receipt(record, receipt)
    prefix = f"formalization/{document_id}"

    def encoded(value: Any) -> bytes:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()

    published = save_receipt(
        record,
        {
            **{key: value for key, value in receipt.items() if key != "receipt_id"},
            "status": "published",
        },
        select=False,
    )
    outputs = {name: content.encode() for name, content in checked["files"].items()}
    outputs.update(
        {
            f"{prefix}/revisions/{revision}.json": encoded(
                {**revision_content(record), "revision_id": revision}
            ),
            f"{prefix}/receipts/{revision}.json": encoded(published),
            f"{prefix}/current.json": encoded({"schema_version": 1, "revision_id": revision}),
            f"{prefix}/handoff.json": encoded(handoff),
            f"{prefix}/Blueprint.md": guidance.encode(),
        }
    )
    for event in receipt_events(record, published["receipt_id"]):
        outputs[f"{prefix}/receipt-history/{revision}/{event['receipt_id']}.json"] = encoded(event)
    for key in (
        set(record["source_files"].values())
        | set(record.get("lean_files", {}).values())
        | set(record.get("context_lean_files", {}).values())
    ):
        outputs[f"{prefix}/blobs/{key}"] = read_blob(store / "blobs", key)
    for path in sorted((store / "review-cache").glob("*.json")):
        cached = json.loads(path.read_text())
        if cached.get("cache_key") != path.stem or cached.get("status") != "completed":
            raise ValueError("Invalid durable source review decision")
        outputs[f"{prefix}/review-cache/{path.name}"] = path.read_bytes()
    original = {**baseline, **metadata_baseline}
    before = {name: original.get(name) for name in outputs}
    plan = freeze_publication_plan(
        revision,
        before,
        outputs,
        owned_paths=set(outputs),
        protected_paths=set(record["source_files"]),
    )
    durable_json(store / "publication-plans" / f"{revision}.json", plan)

    def validate() -> None:
        assert_selection_authority()
        assert_complete_slice(root, record)
        if load_document(root, document_id)["revision_id"] != revision:
            raise ValueError("Selected formalization revision changed before publication")
        if load_receipt(record) != receipt:
            raise ValueError("Selected formalization receipt changed before publication")
        assert_source_current(root, record)
        if lean_environment_generation(root) != checked["environment_generation"]:
            raise ValueError("Project/dependency inputs changed after review")

    def validate_commit() -> None:
        from leanflow_cli.native.harness_artifacts import source_files

        assert_selection_authority()

        if load_document(root, document_id)["revision_id"] != revision:
            raise ValueError("Selected revision changed during publication")
        if load_receipt(record) != receipt:
            raise ValueError("Selected formalization receipt changed during publication")
        assert_source_current(root, record)
        expected = {
            **record["project_source_hashes"],
            f"{prefix}/Blueprint.md": digest(outputs[f"{prefix}/Blueprint.md"]),
        }
        if {name: digest(data) for name, data in source_files(root).items()} != expected:
            raise ValueError("Project inputs changed during publication")
        if environment_identity(root) != record["environment_id"]:
            raise ValueError("Compiler/dependency inputs changed during publication")

    with publication_guard(root), document_lock(root, document_id):
        try:
            journal = publish_revision(
                root=root,
                directory=directory,
                revision_id=revision,
                before=before,
                outputs=outputs,
                plan=plan,
                validate=validate,
                validate_commit=validate_commit,
                cancelled=cancelled,
            )
        except PublicationUnsupported as exc:
            return save_receipt(
                record,
                {
                    **{key: value for key, value in receipt.items() if key != "receipt_id"},
                    "publication_error": str(exc),
                },
            )
        if journal["status"] != "COMMITTED":
            raise ValueError("Publication did not produce its durable commit marker")
        return save_receipt(record, published)
