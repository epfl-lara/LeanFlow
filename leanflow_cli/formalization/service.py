"""Expose revision-bound formalization snapshots, repairs, and frozen editor artifacts."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.actions import apply_action
from leanflow_cli.formalization.corrections import correction_authority_history
from leanflow_cli.formalization.evidence import (
    contained,
    durable_json,
    identifier,
)
from leanflow_cli.formalization.inventory_authority import display_inventory
from leanflow_cli.formalization.records import (
    DocumentSelectionConflict,
    create_document,
    document_lock,
    load_document,
    load_receipt,
    new_revision,
    rehydrate_document,
    save_revision,
    selection_pointer,
)
from leanflow_cli.formalization.service_snapshot import snapshot as snapshot


def inspect_document(
    root: Path,
    source: Path,
    *,
    companion_pdf: Path | None = None,
    verify_current: bool = True,
) -> dict[str, Any]:
    """Freeze intake and return the source audit without launching a model or creating Lean files."""
    root = root.resolve()
    selected = source if source.is_absolute() else root / source
    if selected.is_dir():
        from leanflow_cli.formalization.formalization_documents import (
            resolve_formalization_document,
        )

        selected, _, _ = resolve_formalization_document(root, root, str(selected))
    return snapshot(
        root,
        create_document(
            root, selected, companion_pdf=companion_pdf, reconcile_receipt=verify_current
        ),
        verify_current=verify_current,
    )


def show_document(root: Path, document_id: str, *, verify_current: bool = True) -> dict[str, Any]:
    """Read the selected private revision, falling back to portable published evidence."""
    try:
        record = load_document(root, identifier(document_id), reconcile_receipt=verify_current)
    except DocumentSelectionConflict as error:
        record = load_document(
            root, identifier(document_id), error.private_revision, reconcile_receipt=verify_current
        )
        record["_selection_conflict"] = {
            "message": str(error),
            "private_revision_id": error.private_revision,
            "published_revision_id": error.published_revision,
        }
    result = snapshot(root.resolve(), record, verify_current=verify_current)
    if verify_current and not record.get("_selection_conflict"):
        selected = load_document(root, identifier(document_id))
        if selected["revision_id"] != record["revision_id"]:
            raise ValueError("Formalization revision changed during currentness checking; refresh")
    return result


def list_documents(root: Path, *, verify_current: bool = True) -> dict[str, Any]:
    """List controller-owned documents through the public snapshot protocol."""
    from leanflow_cli.formalization.publication_case_paths import exact_control_path

    ids: set[str] = set()
    for prefix in (".leanflow/formalizations", "formalization"):
        if not exact_control_path(root, prefix):
            continue
        directory = contained(root.resolve(), prefix)
        if directory.exists():
            for path in directory.iterdir():
                if path.is_dir() and not path.is_symlink() and (path / "current.json").is_file():
                    ids.add(identifier(path.name))
    if verify_current:
        documents = [show_document(root, key) for key in sorted(ids)]
    else:
        documents = []
        for key in sorted(ids):
            try:
                record = load_document(root, key, reconcile_receipt=False)
            except DocumentSelectionConflict as error:
                record = load_document(root, key, error.private_revision, reconcile_receipt=False)
            inventory = display_inventory(record, reviewed=False)
            receipt = load_receipt(record)
            documents.append(
                {
                    "schema_version": 1,
                    "document_id": key,
                    "revision_id": record["revision_id"],
                    "status": (receipt or {}).get(
                        "status", record.get("status", "inventory_pending")
                    ),
                    "currentness": "historical",
                    "source_currentness": "unchecked",
                    "source": {
                        "kind": inventory["source_kind"],
                        "path": inventory["source_relative"],
                    },
                    "items": [],
                    "regions": [],
                    "artifacts": [],
                    "diagnostics": [],
                    "counts": {
                        "items": len(inventory["items"]),
                        "regions": len(inventory["regions"]),
                    },
                    "can_prove": False,
                    "handoff": None,
                }
            )
    return {"schema_version": 1, "documents": documents}


def act_document(
    root: Path,
    document_id: str,
    expected_revision: str,
    action: dict[str, Any],
    *,
    acknowledge_only: bool = False,
) -> dict[str, Any]:
    """Serialize a typed edit against the exact visible revision and invalidate prior acceptance."""
    root = root.resolve()
    if not isinstance(action, dict):
        raise ValueError("Formalization action must be a JSON object")
    from leanflow_cli.formalization.publication import publication_guard

    deadline = time.monotonic() + 15 if acknowledge_only else None
    with (
        publication_guard(root, deadline=deadline),
        document_lock(root, document_id, deadline=deadline) as directory,
    ):
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
            current_history = correction_authority_history(record)
            historical = correction_authority_history(selected)
            if historical != current_history:
                if historical != current_history[: len(historical)]:
                    raise ValueError(
                        "Revision selection cannot replace divergent trusted correction authority history"
                    )
                selected = new_revision(selected)
                selected["correction_authorities"] = current_history
                selected = save_revision(directory, selected)
            durable_json(
                contained(directory, "current.json"),
                selection_pointer(directory, selected["revision_id"], revoke_authority=True),
            )
            selected = load_document(root, document_id)
        else:
            selected = new_revision(record)
            apply_action(selected, action)
            selected = save_revision(directory, selected)
    if acknowledge_only:
        return {
            "schema_version": 1,
            "committed": True,
            "document_id": selected["document_id"],
            "revision_id": selected["revision_id"],
            "previous_revision_id": expected_revision,
            "currentness": "unchecked",
        }
    return snapshot(root, selected)


def resolve_artifact(
    root: Path, document_id: str, expected_revision: str, artifact_id: str
) -> dict[str, Any]:
    """Resolve immutable viewer artifacts separately from long controller verification ownership."""
    from leanflow_cli.formalization.service_viewer import resolve_artifact as open_artifact

    return open_artifact(root, document_id, expected_revision, artifact_id)
