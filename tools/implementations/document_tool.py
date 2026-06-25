"""Project-local document inspection tools for formalization workflows."""

from __future__ import annotations

import json
import os

from leanflow_cli.formalization.formalization_documents import inspect_formalization_document
from tools.registry import registry

PDF_READ_DEFAULT_MAX_CHARS = 24_000
PDF_READ_MAX_CHARS = 120_000


def check_document_requirements() -> bool:
    return True


def formalization_document_inspect_tool(
    path: str,
    *,
    cwd: str = "",
    project_root: str = "",
    include_text: bool = False,
) -> str:
    root = project_root or os.getenv("LEANFLOW_PROJECT_ROOT", "") or cwd or os.getcwd()
    payload = inspect_formalization_document(
        path,
        project_root=root,
        cwd=cwd or root,
    )
    if not include_text:
        payload.pop("extracted_text", None)
    return json.dumps(payload, ensure_ascii=False)


def read_pdf_tool(
    path: str,
    *,
    cwd: str = "",
    project_root: str = "",
    max_chars: int = PDF_READ_DEFAULT_MAX_CHARS,
) -> str:
    """Extract and return text from a project-local PDF file with bounded output. Validates input is a PDF, truncates extracted text to max_chars (bounded 1k–120k), and returns metadata including section structure, extraction quality, and truncation flags."""
    root = project_root or os.getenv("LEANFLOW_PROJECT_ROOT", "") or cwd or os.getcwd()
    payload = inspect_formalization_document(
        path,
        project_root=root,
        cwd=cwd or root,
    )
    if payload.get("source_kind") != "pdf":
        return json.dumps(
            {
                "success": False,
                "error": "read_pdf only accepts project-local .pdf files",
                "source_kind": payload.get("source_kind"),
                "source_relative": payload.get("source_relative"),
            },
            ensure_ascii=False,
        )
    try:
        limit = int(max_chars or PDF_READ_DEFAULT_MAX_CHARS)
    except Exception:
        limit = PDF_READ_DEFAULT_MAX_CHARS
    limit = max(1_000, min(limit, PDF_READ_MAX_CHARS))
    extracted = str(payload.get("extracted_text", "") or "")
    payload["extracted_text"] = extracted[:limit]
    payload["text_truncated"] = len(extracted) > limit
    payload["text_limit_chars"] = limit
    return json.dumps(payload, ensure_ascii=False)


FORMALIZATION_DOCUMENT_INSPECT_SCHEMA = {
    "name": "formalization_document_inspect",
    "description": (
        "Inspect a project-local .tex or .pdf source document for formalization planning. "
        "For project-local PDFs, this uses local pdftotext/pdfinfo/pdfimages when installed. "
        "Returns sections, theorem-like LaTeX blocks, labels/references/citations, PDF extraction "
        "metadata, text excerpts, and degraded extraction reasons. Use read_pdf when the immediate "
        "task is simply to read/extract text from a PDF."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project-local .tex or .pdf path"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "project_root": {"type": "string", "description": "Optional LeanFlow project root"},
            "include_text": {
                "type": "boolean",
                "description": "Include the bounded extracted text cache in addition to the excerpt",
                "default": False,
            },
        },
        "required": ["path"],
    },
}


READ_PDF_SCHEMA = {
    "name": "read_pdf",
    "description": (
        "Read/extract text from a project-local PDF file. Use this whenever the model needs to read "
        "a local PDF paper, source document, or formalization reference. Requires local PDF utilities "
        "such as pdftotext; reports degraded reasons when extraction tools are missing or fail. "
        "Returns PDF metadata, detected sections, extracted text, and whether the text was truncated."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project-local .pdf path"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "project_root": {"type": "string", "description": "Optional LeanFlow project root"},
            "max_chars": {
                "type": "integer",
                "description": "Maximum extracted-text characters to return, capped at 120000",
                "default": PDF_READ_DEFAULT_MAX_CHARS,
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
registry.register(
    name="read_pdf",
    toolset="document",
    schema=READ_PDF_SCHEMA,
    handler=lambda args, **kw: read_pdf_tool(
        path=args.get("path", ""),
        cwd=args.get("cwd", ""),
        project_root=args.get("project_root", ""),
        max_chars=int(
            args.get("max_chars", PDF_READ_DEFAULT_MAX_CHARS) or PDF_READ_DEFAULT_MAX_CHARS
        ),
    ),
    check_fn=check_document_requirements,
    emoji="",
)
