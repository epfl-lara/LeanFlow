"""Expose shared project preparation with streaming progress and actionable failures."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from leanflow_cli.cli.cli_handlers import _project_payload
from leanflow_cli.config import load_config
from leanflow_cli.workflows.project import (
    ProjectNotFoundError,
    discover_leanflow_project,
    resolve_template_source,
)
from leanflow_cli.workflows.project_setup import inspect_project, prepare_project
from leanflow_cli.workflows.project_setup.process import safe_text


def register_project_parser(subparsers: Any) -> None:
    """Register setup and its existing init/create spellings against one pipeline."""
    parser = subparsers.add_parser("project", help="Set up or inspect LeanFlow projects")
    commands = parser.add_subparsers(dest="project_command")
    for command, help_text in (
        ("setup", "Prepare a new or existing Lean project (Mathlib by default)"),
        ("init", "Set up a project; alias for project setup"),
        ("create", "Create and prepare a project, optionally from a template"),
    ):
        child = commands.add_parser(command, help=help_text)
        if command == "create":
            child.add_argument("path")
            child.add_argument("--template-source", default="")
        else:
            child.add_argument("path", nargs="?", default=".")
        child.add_argument(
            "--repair",
            action="store_true",
            help="Recheck dependencies and rebuild verification tools, bypassing cached readiness",
        )
        child.add_argument("--name", default="")
        child.add_argument("--preset", choices=("mathlib", "lean"), default="mathlib")
        child.add_argument("--source-file", default="", help="Copy a Lean file into a new project")
        child.add_argument(
            "--json", action="store_true", dest="json_output", help="Stream JSON events"
        )
    inspect = commands.add_parser("inspect", help="Inspect project setup without changing files")
    inspect.add_argument("path", nargs="?", default=".")
    inspect.add_argument("--json", action="store_true", dest="json_output")
    show = commands.add_parser("show", help="Show the registered project")
    show.add_argument("path", nargs="?", default=".")


def setup_exit_code(result: dict[str, Any]) -> int:
    """Keep cancellation distinct from a failed preparation or a ready environment."""
    return (
        0 if result.get("status") == "ready" else 130 if result.get("status") == "cancelled" else 1
    )


def setup_result_lines(result: dict[str, Any]) -> list[str]:
    """Render a short outcome while retaining the failed phase and recovery path."""
    if result.get("status") == "ready":
        return [f"Ready to prove: {result.get('root', '')}"]
    label = "Setup cancelled" if result.get("status") == "cancelled" else "Setup failed"
    lines = [f"{label} at {result.get('phase', 'setup')}: {result.get('message', '')}"]
    for key, prefix in (("remediation", "Next step"), ("log_path", "Log")):
        if result.get(key):
            lines.append(f"{prefix}: {result[key]}")
    return lines


def execute_setup(
    args: argparse.Namespace, *, emit: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    """Translate terminal cancellation into cooperative owned-process cleanup."""
    cancel = threading.Event()
    handlers: dict[signal.Signals, Any] = {}

    def stop(_signum: int, _frame: FrameType | None) -> None:
        cancel.set()

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, stop)
    try:
        template = getattr(args, "template_source", "")
        if args.project_command == "create" and not template:
            template = resolve_template_source(load_config(), os.environ)
        return prepare_project(
            args.path,
            name=args.name or None,
            preset=args.preset,
            repair=args.repair,
            source_file=args.source_file or None,
            template_source=template or None,
            progress=emit,
            cancel=cancel,
        )
    except (OSError, ValueError) as error:
        # Even failures before a log can be created must use the editor protocol.
        return _io_failure(args.path, error)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def _io_failure(path: str, error: Exception) -> dict[str, Any]:
    """Keep inaccessible configuration and state failures actionable in every UI."""
    return {
        "status": "failed",
        "phase": "checking",
        "code": "setup_io_failed",
        "message": safe_text(str(error)),
        "remediation": "Check folder permissions, configuration, and disk space, then retry setup.",
        "root": str(Path(path).expanduser().absolute()),
        "log_path": "",
        "changed_files": [],
        "capabilities": {},
    }


def handle_project(args: argparse.Namespace) -> int:
    """Run project inspection, registration display, or the shared setup pipeline."""
    if args.project_command == "show":
        try:
            project = discover_leanflow_project(args.path)
        except ProjectNotFoundError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(_project_payload(project), indent=2, sort_keys=True))
        return 0
    if args.project_command == "inspect":
        try:
            report = inspect_project(args.path)
        except (OSError, ValueError) as error:
            report = {**_io_failure(args.path, error), "setup_status": "failed", "ready": False}
        if args.json_output:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"Project: {report.get('root', str(Path(args.path).resolve()))}")
            print(f"Kind: {report.get('kind', 'unknown')}")
            print(f"Setup: {report.get('setup_status', 'unknown')}")
            if report.get("message"):
                print(report["message"])
        return 0
    if args.project_command not in {"setup", "init", "create"}:
        raise SystemExit("Choose project setup, inspect, or show.")

    def emit(event: dict[str, Any]) -> None:
        if args.json_output:
            print(json.dumps(event, sort_keys=True), flush=True)
        elif event.get("message"):
            print(event["message"], flush=True)

    result = execute_setup(args, emit=emit)
    if args.json_output:
        emit({**result, "type": "result"})
    else:
        for line in setup_result_lines(result):
            print(line)
    return setup_exit_code(result)
