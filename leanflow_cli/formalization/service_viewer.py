"""Publish immutable viewer copies without queuing behind controller verification locks."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, digest, identifier
from leanflow_cli.formalization.publication_case_paths import assert_receipt_control_path
from leanflow_cli.formalization.publication_io import rename_exclusive
from leanflow_cli.formalization.records import (
    document_directory,
    document_lock,
    load_document,
    load_receipt,
)
from leanflow_cli.formalization.service_artifacts import _artifacts, _display_preview
from leanflow_cli.formalization.source_draft_artifacts import read_regular

MAX_VIEWER_BYTES = 64 * 1024 * 1024


def _selected_artifact(
    root: Path,
    document_id: str,
    expected_revision: str,
    artifact_id: str,
    *,
    proof: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Authenticate the selected revision and its exact immutable artifact allowlist."""
    record = load_document(root, document_id, reconcile_receipt=proof)
    if record["revision_id"] != identifier(expected_revision):
        raise ValueError("Formalization revision changed; refresh before opening its source")
    proof_receipt = None
    if proof:
        from leanflow_cli.formalization.proof_receipts import current_proof_receipt

        try:
            proof_receipt = current_proof_receipt(root, record, load_receipt(record))
        except (OSError, ValueError, RuntimeError):
            proof_receipt = None
    preview = _display_preview(root, record, verify_current=proof)[0]
    candidates = [
        artifact
        for artifact in _artifacts(record, proof_receipt, preview)
        if artifact["id"] == artifact_id
    ]
    if len(candidates) != 1:
        raise ValueError("Artifact does not belong to this selected revision")
    return record, candidates[0], preview


def _checked_bytes(path: Path, key: str) -> bytes:
    """Reject oversized, aliased, changed or hash-mismatched viewer inputs before copying."""
    data = read_regular(path, MAX_VIEWER_BYTES)
    if digest(data) != key:
        raise ValueError("Frozen viewer artifact was modified")
    return data


def _materialize(directory: Path, artifact: dict[str, Any], data: bytes) -> dict[str, Any]:
    """Atomically publish one read-only viewer file; preserve any differing concurrent entry."""
    relative = f"viewer/{identifier(artifact['id'])}{Path(artifact['path']).suffix}"
    assert_receipt_control_path(directory, relative)
    target = contained(directory, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _checked_bytes(target, artifact["sha256"])
    except FileNotFoundError:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".viewer-", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), 0o444)
                os.fsync(handle.fileno())
            assert_receipt_control_path(directory, relative)
            contained(directory, relative)
            try:
                rename_exclusive(temporary, target)
            except FileExistsError:
                # Another viewer may publish the same complete bytes first.
                pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        _checked_bytes(target, artifact["sha256"])
    return {
        key: value for key, value in {**artifact, "path": str(target)}.items() if key != "origin"
    }


def _open_selected(
    root: Path,
    document_id: str,
    expected_revision: str,
    artifact_id: str,
    *,
    proof: bool = False,
) -> dict[str, Any]:
    """Open selected bytes, requiring project ownership separately from frozen viewer publication."""
    record, artifact, preview = _selected_artifact(
        root, document_id, expected_revision, artifact_id, proof=proof
    )
    if artifact["kind"] == "project":
        from leanflow_cli.formalization.draft_delivery import resolve_project_artifact

        if artifact["origin"] != "draft" or preview is None:
            raise ValueError("Project artifact requires a retained draft preview")
        project = resolve_project_artifact(root, preview, artifact)
        return {
            key: value
            for key, value in {**artifact, "path": str(project)}.items()
            if key != "origin"
        }
    artifact_directory = (
        document_directory(root, document_id)
        if artifact["origin"] == "draft"
        else Path(record["_directory"])
    )
    if artifact["origin"] == "proof":
        original = contained(root, artifact["path"])
    else:
        relative = f"blobs/{artifact['sha256']}"
        assert_receipt_control_path(artifact_directory, relative)
        original = contained(artifact_directory, relative)
    data = _checked_bytes(original, artifact["sha256"])
    opened = _materialize(document_directory(root, document_id), artifact, data)
    if not proof:
        # Immutable bytes need no mutation lock. Recheck selection and preview
        # membership after publication so a concurrent edit cannot return an old link.
        _, current, _ = _selected_artifact(root, document_id, expected_revision, artifact_id)
        if current != artifact:
            raise ValueError("Frozen artifact selection changed; refresh before opening")
    return opened


def resolve_artifact(
    root: Path, document_id: str, expected_revision: str, artifact_id: str
) -> dict[str, Any]:
    """Read frozen evidence without controller locks; retain full live-proof/project guards."""
    from leanflow_cli.formalization.publication import publication_guard
    from leanflow_cli.formalization.publication_io import publication_lock

    root = root.resolve()
    if artifact_id.startswith("proof-lean-"):
        with publication_guard(root), document_lock(root, document_id):
            return _open_selected(root, document_id, expected_revision, artifact_id, proof=True)
    _, artifact, _ = _selected_artifact(root, document_id, expected_revision, artifact_id)
    if artifact["kind"] == "project":
        with publication_lock(root), document_lock(root, document_id):
            return _open_selected(root, document_id, expected_revision, artifact_id)
    return _open_selected(root, document_id, expected_revision, artifact_id)
