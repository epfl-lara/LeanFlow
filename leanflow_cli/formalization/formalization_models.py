"""Define immutable data and errors for document formalization."""

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
            "LEANFLOW_FORMALIZATION_DOCUMENT": str(self.source_path),
            "LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE": self.source_relative,
            "LEANFLOW_FORMALIZATION_REQUEST_KIND": str(
                self.metadata.get("document_request_kind", "file") or "file"
            ),
            "LEANFLOW_FORMALIZATION_REQUEST_RELATIVE": str(
                self.metadata.get("document_request_relative", self.source_relative)
                or self.source_relative
            ),
            "LEANFLOW_FORMALIZATION_SELECTED_SOURCE": self.source_relative,
            "LEANFLOW_FORMALIZATION_DOCUMENT_KIND": self.source_kind,
            "LEANFLOW_FORMALIZATION_CONTEXT": str(self.context_path),
            "LEANFLOW_WORKFLOW_CONTEXT": str(self.context_path),
            "LEANFLOW_FORMALIZATION_MANIFEST": str(self.manifest_path),
            "LEANFLOW_FORMALIZATION_BLUEPRINT": str(self.blueprint_path),
            "LEANFLOW_FORMALIZATION_BLUEPRINT_SKILL": str(self.blueprint_skill_path),
            "LEANFLOW_FORMALIZATION_EXTRACTED_TEXT": str(self.extracted_text_path),
            "LEANFLOW_FORMALIZATION_TARGET_FILE": self.target_lean_relative,
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
