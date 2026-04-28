"""Project-local document inspection tools for formalization workflows."""

from __future__ import annotations

import json
import os
from pathlib import Path

from epflemma_cli.formalization_documents import inspect_formalization_document
from tools.registry import registry


def check_document_requirements() -> bool:
    return True


def formalization_document_inspect_tool(
    path: str,
    *,
    cwd: str = "",
    project_root: str = "",
    include_text: bool = False,
) -> str:
    root = (
        project_root
        or os.getenv("EPFLEMMA_PROJECT_ROOT", "")
        or os.getenv("OPENGAUSS_PROJECT_ROOT", "")
        or cwd
        or os.getcwd()
    )
    payload = inspect_formalization_document(
        path,
        project_root=root,
        cwd=cwd or root,
    )
    if not include_text:
        payload.pop("extracted_text", None)
    return json.dumps(payload, ensure_ascii=False)


FORMALIZATION_DOCUMENT_INSPECT_SCHEMA = {
    "name": "formalization_document_inspect",
    "description": (
        "Inspect a project-local .tex or .pdf source document for formalization planning. "
        "Returns sections, theorem-like LaTeX blocks, labels/references/citations, PDF extraction "
        "metadata, text excerpts, and degraded extraction reasons."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project-local .tex or .pdf path"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "project_root": {"type": "string", "description": "Optional EPFLemma project root"},
            "include_text": {
                "type": "boolean",
                "description": "Include the bounded extracted text cache in addition to the excerpt",
                "default": False,
            },
        },
        "required": ["path"],
    },
}


registry.register(
    name="formalization_document_inspect",
    toolset="document",
    schema=FORMALIZATION_DOCUMENT_INSPECT_SCHEMA,
    handler=lambda args, **kw: formalization_document_inspect_tool(
        path=args.get("path", ""),
        cwd=args.get("cwd", ""),
        project_root=args.get("project_root", ""),
        include_text=bool(args.get("include_text", False)),
    ),
    check_fn=check_document_requirements,
    emoji="",
)
