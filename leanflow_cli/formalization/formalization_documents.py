"""Resolve document requests and expose the current inventory and intake interfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization.formalization_models import (
    FormalizationDocumentContext,
    FormalizationDocumentError,
    _FormalizationDocumentSelection,
)
from leanflow_cli.formalization.formalization_tex_discovery import (
    _discover_tex_project_entrypoint,
    _project_local_existing_path,
    _strip_wrapping_quotes,
)
from leanflow_cli.formalization.source_display import display_inventory

SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS = {
    ".tex": "latex",
    ".pdf": "pdf",
}


def _select_formalization_document(
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
) -> _FormalizationDocumentSelection:
    """Parse and resolve a formalization document path (file or directory) from a user request, validating extension and project locality. Returns a _FormalizationDocumentSelection with resolved source path, kind (latex/pdf), and discovery metadata; raises FormalizationDocumentError on invalid input."""
    root = Path(project_root).expanduser().resolve()
    base = Path(cwd).expanduser().resolve()
    raw = _strip_wrapping_quotes(workflow_args)
    if not raw:
        raise FormalizationDocumentError(
            "formalize requires a project-local .tex source, .pdf source, or TeX project directory, "
            "for example `/formalize docs/paper.tex` or `/autoformalize docs/paper`."
        )
    if raw.startswith("-"):
        raise FormalizationDocumentError(
            "formalize requires the document path before options, for example `/formalize docs/paper.tex --prompt section 2`."
        )

    raw_suffix = Path(raw).suffix.lower()
    if raw_suffix == ".lean":
        raise FormalizationDocumentError(
            "formalize expects a source document (.tex or .pdf) or TeX project directory. "
            "Use `/prove` for an existing Lean file, or select its informal source with `/formalize` "
            "to translate statements before proving."
        )
    try:
        requested_path, requested_relative = _project_local_existing_path(root, base, raw)
    except FormalizationDocumentError:
        if raw_suffix and raw_suffix not in SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS:
            raise FormalizationDocumentError(
                "formalize requires a project-local .tex source, .pdf source, or directory containing a TeX project. "
                f"Unsupported document extension: {raw_suffix}"
            ) from None
        if not raw_suffix:
            raise FormalizationDocumentError(
                "formalize requires a project-local .tex source, .pdf source, or directory containing a TeX project, "
                "for example `/formalize docs/paper.tex` or `/autoformalize docs/paper`."
            ) from None
        raise
    if requested_path.is_dir():
        return _discover_tex_project_entrypoint(root, requested_path)

    suffix = requested_path.suffix.lower()
    if suffix not in SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS:
        raise FormalizationDocumentError(
            "formalize requires a project-local .tex source, .pdf source, or directory containing a TeX project. "
            f"Unsupported document extension: {suffix or '[none]'}"
        )
    kind = SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS[suffix]
    return _FormalizationDocumentSelection(
        source_path=requested_path,
        source_relative=requested_relative,
        source_kind=kind,
        request_path=requested_path,
        request_relative=requested_relative,
        request_kind="file",
        discovery_metadata={
            "document_request_kind": "file",
            "document_request_path": str(requested_path),
            "document_request_relative": requested_relative,
            "selected_source_document": str(requested_path),
            "selected_source_document_relative": requested_relative,
        },
    )


def resolve_formalization_document(
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
) -> tuple[Path, str, str]:
    """Resolve and validate the required document path for `/formalize`."""

    selection = _select_formalization_document(project_root, cwd, workflow_args)
    return selection.source_path, selection.source_relative, selection.source_kind


def inspect_formalization_document(
    path: str | Path,
    *,
    project_root: str | Path | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Project one complete inventory for document tools without re-extracting source."""
    from leanflow_cli.formalization.source_inventory import inventory_source

    base = Path(cwd or Path.cwd()).expanduser().resolve()
    root = Path(project_root).expanduser().resolve() if project_root else base
    selection = _select_formalization_document(root, base, str(path))
    inventory = inventory_source(root, selection.source_path)
    return {
        **display_inventory(inventory),
        **selection.discovery_metadata,
        "success": True,
        "source_path": str(selection.source_path),
        "source_relative": selection.source_relative,
        "source_kind": selection.source_kind,
        "document_request_kind": selection.request_kind,
        "document_request_path": str(selection.request_path),
        "document_request_relative": selection.request_relative,
        "inventory": inventory,
        "extraction_status": inventory["extraction_status"],
        "degraded_reasons": [entry["message"] for entry in inventory["diagnostics"]],
    }


def prepare_formalization_document_context(
    *,
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
    require_preparation: bool = False,
    source_draft_only: bool = False,
) -> FormalizationDocumentContext:
    """Freeze source intake and allocate prospective modules without editing project sources."""
    from leanflow_cli.formalization.intake import prepare_intake

    root = Path(project_root).expanduser().resolve()
    selection = _select_formalization_document(
        root, Path(cwd).expanduser().resolve(), workflow_args
    )
    return prepare_intake(
        root,
        selection,
        require_preparation=require_preparation,
        source_draft_only=source_draft_only,
    )
