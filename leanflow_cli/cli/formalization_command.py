"""Expose source intake, exact-revision repairs, and editor artifacts through one CLI API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.service import (
    act_document,
    inspect_document,
    list_documents,
    resolve_artifact,
    show_document,
)


def register_formalization_parser(subparsers: Any) -> None:
    """Register deterministic document actions without implicitly launching a model."""
    parser = subparsers.add_parser(
        "formalization", help="Inspect and repair source formalization inventories"
    )
    parser.add_argument("--project", default=".", help="Project owning the source inventory")
    commands = parser.add_subparsers(dest="formalization_command", required=True)
    for name, help_text in (
        ("list", "List frozen source inventories"),
        ("inspect", "Freeze a TeX project or PDF and inspect source coverage"),
        ("show", "Show the selected document revision"),
        ("action", "Apply a source repair against an exact revision"),
        ("artifact", "Resolve a frozen source or Lean artifact on this host"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", dest="json_output")
        if name == "inspect":
            command.add_argument("source")
            command.add_argument(
                "--companion-pdf",
                help="Explicitly associate a PDF with a TeX source for visual audit",
            )
        elif name != "list":
            command.add_argument("document_id")
        if name in {"action", "artifact"}:
            command.add_argument("--expected-revision", required=True)
        if name == "action":
            command.add_argument("--action-json", required=True)
        if name == "artifact":
            command.add_argument("artifact_id")


def handle_formalization(args: argparse.Namespace) -> int:
    """Return JSON snapshots or actionable failures without leaking private session state."""
    try:
        root = Path(args.project).resolve()
        operation = args.formalization_command
        if operation == "list":
            result = list_documents(root)
        elif operation == "inspect":
            companion = getattr(args, "companion_pdf", None)
            result = inspect_document(
                root, Path(args.source), companion_pdf=Path(companion) if companion else None
            )
        elif operation == "show":
            result = show_document(root, args.document_id)
        elif operation == "action":
            result = act_document(
                root, args.document_id, args.expected_revision, json.loads(args.action_json)
            )
        elif operation == "artifact":
            result = resolve_artifact(
                root, args.document_id, args.expected_revision, args.artifact_id
            )
        else:
            raise ValueError("Unknown formalization operation")
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json_output else 2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        result = {"schema_version": 1, "success": False, "error": str(error)}
        print(json.dumps(result) if args.json_output else str(error))
        return 1
