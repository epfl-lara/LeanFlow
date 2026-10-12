"""Expose shared dependency storage and safe maintenance through the CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from leanflow_cli.runtime.dependency_cache import ensure_dependency_entry
from leanflow_cli.runtime.project_operations import project_setup
from leanflow_cli.runtime.storage import cleanup_storage, storage_status


def register_storage_parser(subparsers: Any) -> None:
    """Register storage inspection, collection and explicit cache publication."""
    parser = subparsers.add_parser("storage", help="Inspect and clean shared dependencies")
    commands = parser.add_subparsers(dest="storage_command", required=True)
    for name, help_text in (
        ("status", "Show managed storage and free disk space"),
        ("clean", "Remove unused managed caches; retain proofs and checkpoints"),
        ("share", "Publish a prepared project's pinned dependencies for reuse"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", dest="json_output")
        if name == "share":
            command.add_argument("path", nargs="?", default=".")


def handle_storage(args: argparse.Namespace) -> int:
    """Perform one storage action and return an actionable, credential-free result."""
    try:
        if args.storage_command == "clean":
            cleanup = cleanup_storage()
            payload = {**storage_status(), "cleanup": cleanup}
        elif args.storage_command == "share":
            root = Path(args.path).resolve()
            with project_setup(root):
                entry = ensure_dependency_entry(root)
            payload = {**storage_status(), "shared": entry is not None}
        else:
            payload = storage_status()
        if args.json_output:
            print(json.dumps(payload, default=str))
        else:
            print(json.dumps(payload, indent=2, default=str))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        payload = {"success": False, "error": str(exc)}
        print(json.dumps(payload) if args.json_output else str(exc))
        return 1
