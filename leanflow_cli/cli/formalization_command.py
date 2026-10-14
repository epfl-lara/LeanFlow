"""Expose source intake, exact-revision repairs, and editor artifacts through one CLI API."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.service import (
    act_document,
    inspect_document,
    list_documents,
    resolve_artifact,
    show_document,
)
from leanflow_cli.workflows.project_setup.process import SetupCancelled


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
        ("plan", "Estimate pinned dependencies and project layout before translation"),
        ("prepare", "Acquire and verify document prerequisites in a private project"),
        ("conflicts", "Inspect retained publication versions and current observations"),
        ("resolve-conflict", "Keep explicitly reconciled files and invalidate prior acceptance"),
        ("conflict-artifact", "Open a frozen version from a publication conflict"),
        ("preparation-conflicts", "Inspect preserved project preparation versions"),
        (
            "resolve-preparation",
            "Keep reconciled preparation versions and require a new setup check",
        ),
        (
            "recover-preparation",
            "Recover interrupted preparation under exclusive project ownership",
        ),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", dest="json_output")
        if name == "inspect":
            command.add_argument("source")
            command.add_argument(
                "--companion-pdf",
                help="Explicitly associate a PDF with a TeX source for visual audit",
            )
        elif name not in {
            "list",
            "conflicts",
            "resolve-conflict",
            "conflict-artifact",
            "preparation-conflicts",
            "resolve-preparation",
            "recover-preparation",
        }:
            command.add_argument("document_id")
        if name in {"action", "artifact", "plan", "prepare"}:
            command.add_argument("--expected-revision", required=True)
        if name in {"plan", "prepare"}:
            command.add_argument("--preset", choices=("mathlib", "lean"), default="mathlib")
            command.add_argument("--plan-json")
        if name == "prepare":
            command.add_argument("--timeout-s", type=float, default=1800)
            command.add_argument("--offline", action="store_true")
        if name == "conflicts":
            command.add_argument("--transaction-id")
            command.add_argument("--version-offset", type=int, default=0)
        if name in {"resolve-conflict", "resolve-preparation"}:
            command.add_argument("--resolution-json", required=True)
        if name == "conflict-artifact":
            command.add_argument("transaction_id")
            command.add_argument("version_id")
            command.add_argument("--expected-observation", required=True)
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
        elif operation in {"preparation-conflicts", "resolve-preparation", "recover-preparation"}:
            from leanflow_cli.formalization.preparation_transaction import (
                preparation_conflicts,
                recover_preparation,
                resolve_preparation_conflict,
            )
            from leanflow_cli.runtime.project_operations import project_setup

            if operation == "preparation-conflicts":
                result = preparation_conflicts(root)
            else:
                with project_setup(root):
                    if operation == "recover-preparation":
                        result = recover_preparation(root) or {"status": "no_pending_preparation"}
                    else:
                        request = json.loads(args.resolution_json)
                        if not isinstance(request, dict) or set(request) != {
                            "observation",
                            "reason",
                        }:
                            raise ValueError(
                                "Preparation resolution requires exact observation and reason"
                            )
                        result = resolve_preparation_conflict(
                            root, request["observation"], request["reason"]
                        )
        elif operation in {"plan", "prepare"}:
            from leanflow_cli.cli.formalization_preparation import prepare_operation

            result = prepare_operation(root, args)
        elif operation in {"conflicts", "resolve-conflict", "conflict-artifact"}:
            from leanflow_cli.formalization.conflict_resolution import (
                publication_conflicts,
                resolve_conflict_artifact,
                resolve_publication_conflict,
            )

            if operation == "conflicts":
                result = publication_conflicts(
                    root, transaction_id=args.transaction_id, version_offset=args.version_offset
                )
            elif operation == "resolve-conflict":
                result = resolve_publication_conflict(root, json.loads(args.resolution_json))
            else:
                result = resolve_conflict_artifact(
                    root, args.transaction_id, args.expected_observation, args.version_id
                )
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
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        SetupCancelled,
    ) as error:
        message = (
            "Project preparation was cancelled" if isinstance(error, SetupCancelled) else str(error)
        )
        result = {"schema_version": 1, "success": False, "error": message}
        print(json.dumps(result) if args.json_output else message)
        return 1
