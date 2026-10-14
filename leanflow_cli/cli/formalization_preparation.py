"""Plan and prepare one frozen document with bounded progress and signal cancellation."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.preparation import prepare_document_project
from leanflow_cli.formalization.preparation_plan import default_preparation_plan
from leanflow_cli.formalization.records import assert_source_current, document_lock, load_document
from leanflow_cli.formalization.service import snapshot


def prepare_operation(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Bind planning or setup to the selected revision; keep progress off JSON stdout."""
    with document_lock(root, args.document_id):
        record = load_document(root, args.document_id)
        if record["revision_id"] != args.expected_revision:
            raise ValueError("Source selection changed; refresh before preparing the project")
        assert_source_current(root, record)
        overrides = json.loads(args.plan_json) if args.plan_json else None
        if overrides is not None and not isinstance(overrides, dict):
            raise ValueError("Preparation overrides must be a JSON object")
        plan = default_preparation_plan(root, record, preset=args.preset, overrides=overrides)
        if args.formalization_command == "plan":
            return {"schema_version": 1, "plan": plan, "semantic_approval": False}
        cancel = threading.Event()
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda *_: cancel.set())

        def progress(event: dict[str, Any]) -> None:
            print(
                json.dumps({"type": "preparationProgress", **event}, ensure_ascii=False),
                file=sys.stderr,
                flush=True,
            )

        try:
            result = prepare_document_project(
                root,
                record,
                plan=plan,
                progress=progress,
                cancel=cancel,
                timeout_s=args.timeout_s,
                network_allowed=not args.offline,
            )
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        latest = load_document(root, args.document_id)
        if latest["revision_id"] != args.expected_revision:
            raise ValueError("Source selection changed during preparation; refresh and restart")
        return {**snapshot(root, latest), "preparation_result": result}
