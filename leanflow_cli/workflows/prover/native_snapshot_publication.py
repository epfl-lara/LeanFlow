"""Omit canonical approval projections from model copies while retaining mathematical source."""

from __future__ import annotations

from pathlib import Path

from leanflow_cli.formalization.publication_registry_inventory import owned_document_paths

PUBLICATION_EXCLUSION = "controller-publication-projections-v1"
_CONTROL_FILES = frozenset(
    {"current.json", "handoff.json", "proof-current.json", "publication.json"}
)
_CONTROL_DIRECTORIES = frozenset(
    {"revisions", "receipts", "proof-revisions", "receipt-history", "review-cache", "blobs"}
)


def publication_documents(root: Path) -> frozenset[str]:
    """Use bounded exact-case ownership shared with canonical publication recovery."""
    return frozenset(path.name for path in owned_document_paths(root))


def exclude_publication_file(relative: Path, documents: frozenset[str]) -> bool:
    """Drop controller artifacts only within known portable document owners.

    Lean sources remain available even when stored under a control-named folder;
    ordinary libraries and useful top-level blueprints remain source context.
    This filtering grants neither publication authority nor mathematical approval.
    """
    parts = relative.parts
    if len(parts) < 3 or parts[0] != "formalization" or parts[1] not in documents:
        return False
    if relative.suffix == ".lean":
        return False
    return (len(parts) == 3 and parts[2] in _CONTROL_FILES) or parts[2] in _CONTROL_DIRECTORIES
