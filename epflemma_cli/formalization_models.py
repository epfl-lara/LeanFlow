"""Typed data + error classes for the formalization-document layer.

Leaf module (frozen dataclasses + the FormalizationDocumentError exception). Extracted from
formalization_documents.py so the document module and the TeX-discovery leaf share one set of
definitions without an import cycle; re-exported from formalization_documents for the historical
`from epflemma_cli.formalization_documents import FormalizationDocument*` surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FormalizationDocumentError(ValueError):
    """Raised when a formalization document request is invalid."""


@dataclass(frozen=True)
class FormalizationDocumentContext:
    source_path: Path
    source_relative: str
    source_kind: str
    context_path: Path
    manifest_path: Path
    extracted_text_path: Path
    blueprint_path: Path
    blueprint_skill_path: Path
    target_lean_path: Path
    target_lean_relative: str
    metadata: dict[str, Any]

    def to_env(self) -> dict[str, str]:
        return {
            "EPFLEMMA_FORMALIZATION_DOCUMENT": str(self.source_path),
            "OPENGAUSS_FORMALIZATION_DOCUMENT": str(self.source_path),
            "EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE": self.source_relative,
            "OPENGAUSS_FORMALIZATION_DOCUMENT_RELATIVE": self.source_relative,
            "EPFLEMMA_FORMALIZATION_REQUEST_KIND": str(self.metadata.get("document_request_kind", "file") or "file"),
            "OPENGAUSS_FORMALIZATION_REQUEST_KIND": str(self.metadata.get("document_request_kind", "file") or "file"),
            "EPFLEMMA_FORMALIZATION_REQUEST_RELATIVE": str(
                self.metadata.get("document_request_relative", self.source_relative) or self.source_relative
            ),
            "OPENGAUSS_FORMALIZATION_REQUEST_RELATIVE": str(
                self.metadata.get("document_request_relative", self.source_relative) or self.source_relative
            ),
            "EPFLEMMA_FORMALIZATION_SELECTED_SOURCE": self.source_relative,
            "OPENGAUSS_FORMALIZATION_SELECTED_SOURCE": self.source_relative,
            "EPFLEMMA_FORMALIZATION_DOCUMENT_KIND": self.source_kind,
            "OPENGAUSS_FORMALIZATION_DOCUMENT_KIND": self.source_kind,
            "EPFLEMMA_FORMALIZATION_CONTEXT": str(self.context_path),
            "OPENGAUSS_FORMALIZATION_CONTEXT": str(self.context_path),
            "EPFLEMMA_WORKFLOW_CONTEXT": str(self.context_path),
            "GAUSS_AUTOFORMALIZE_CONTEXT": str(self.context_path),
            "EPFLEMMA_FORMALIZATION_MANIFEST": str(self.manifest_path),
            "OPENGAUSS_FORMALIZATION_MANIFEST": str(self.manifest_path),
            "EPFLEMMA_FORMALIZATION_BLUEPRINT": str(self.blueprint_path),
            "OPENGAUSS_FORMALIZATION_BLUEPRINT": str(self.blueprint_path),
            "EPFLEMMA_FORMALIZATION_BLUEPRINT_SKILL": str(self.blueprint_skill_path),
            "OPENGAUSS_FORMALIZATION_BLUEPRINT_SKILL": str(self.blueprint_skill_path),
            "EPFLEMMA_FORMALIZATION_EXTRACTED_TEXT": str(self.extracted_text_path),
            "OPENGAUSS_FORMALIZATION_EXTRACTED_TEXT": str(self.extracted_text_path),
            "EPFLEMMA_FORMALIZATION_TARGET_FILE": self.target_lean_relative,
            "OPENGAUSS_FORMALIZATION_TARGET_FILE": self.target_lean_relative,
        }


@dataclass(frozen=True)
class _FormalizationDocumentSelection:
    source_path: Path
    source_relative: str
    source_kind: str
    request_path: Path
    request_relative: str
    request_kind: str
    discovery_metadata: dict[str, Any]
