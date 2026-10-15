"""Publish a verified, cancellable CLI preparation owner before native-controller handoff."""

from __future__ import annotations

import os
import re
import secrets
import signal
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.process_identity import PROCESS_TOKEN_ENV, _process_token, process_token_sha256
from leanflow_cli.runtime.workflow_resources import WorkflowResourceWindow
from leanflow_cli.workflows.workflow_state import (
    mark_workflow_live_status_startup,
    release_workflow_startup_owner,
)

_REEXEC = "leanflow_formalization_preflight_token"


def inherited_process_token() -> bool:
    """Require external visibility instead of the same-process identity shortcut."""
    expected = os.environ.get(PROCESS_TOKEN_ENV, "")
    return bool(
        expected
        and process_token_sha256(_process_token(os.getpid())) == process_token_sha256(expected)
    )


def ensure_cli_preflight_token() -> None:
    """Re-exec an actual formalization CLI once with an initially inherited token."""
    run_id = os.environ.get("LEANFLOW_WORKFLOW_RUN_ID", "").strip()
    if run_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid formalization preflight run identity")
    if inherited_process_token():
        if not run_id:
            os.environ["LEANFLOW_WORKFLOW_RUN_ID"] = "formalize-cli-" + uuid.uuid4().hex[:16]
        return
    if _REEXEC in sys._xoptions:
        raise RuntimeError(
            "Formalization startup token is not externally observable after one re-exec; no preparation or model call started"
        )
    original = list(sys.orig_argv)
    if not original or not all(isinstance(value, str) for value in original):
        raise RuntimeError(
            "Cannot preserve the original Python CLI invocation for verified preparation startup"
        )
    environment = dict(os.environ)
    environment[PROCESS_TOKEN_ENV] = secrets.token_urlsafe(32)
    environment["LEANFLOW_WORKFLOW_RUN_ID"] = run_id or "formalize-cli-" + uuid.uuid4().hex[:16]
    # Preserve -I/-B, -m/-c and literal arguments; exec retains PID, descriptors,
    # session and native authentication while replacing only the initial env.
    try:
        os.execve(
            sys.executable, [sys.executable, "-X", _REEXEC + "=1", *original[1:]], environment
        )
    except OSError as error:
        raise RuntimeError(
            "Cannot restart formalization CLI with a verified startup token; no preparation or model call started"
        ) from error


@contextmanager
def preflight_owner(
    root: Path, environment: Mapping[str, str]
) -> Iterator[tuple[threading.Event, WorkflowResourceWindow]]:
    """Share preparation cancellation/resources and conditionally release only our live pointer."""
    cancelled = threading.Event()
    resources = WorkflowResourceWindow(environment, cancelled)
    keys = ("LEANFLOW_PROJECT_ROOT", "LEANFLOW_WORKFLOW_RUN_ID", "LEANFLOW_NATIVE_WORKFLOW_KIND")
    previous = {name: os.environ.get(name) for name in keys}
    run_id = str(
        environment.get("LEANFLOW_NATIVE_RESUME_RUN_ID")
        or environment.get("LEANFLOW_WORKFLOW_RUN_ID")
        or ""
    )
    if run_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid formalization preflight run identity")
    handlers: dict[int, Any] = {}
    claimed, successful, closed = False, False, False
    try:
        os.environ.update(
            LEANFLOW_PROJECT_ROOT=str(root), LEANFLOW_NATIVE_WORKFLOW_KIND="formalize"
        )
        if run_id:
            os.environ["LEANFLOW_WORKFLOW_RUN_ID"] = run_id
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                handlers[signum] = signal.signal(signum, lambda *_: cancelled.set())
        # Direct API callers keep their existing control contract; only an
        # externally verifiable inherited CLI token can publish Stop authority.
        if run_id and inherited_process_token():
            mark_workflow_live_status_startup(
                phase="starting",
                metadata={
                    "run_id": run_id,
                    "project_root": str(root),
                    "workflow_kind": "formalize",
                    "workflow_command": "/formalize",
                    "status": "running",
                    "terminal": False,
                    "owner_role": "formalization-preflight",
                },
            )
            claimed = True
        resources.start()
        if cancelled.is_set():
            raise ValueError("Formalization preparation cancelled before admission")
        yield cancelled, resources
        resources.check(force=True)
        if cancelled.is_set():
            raise ValueError("Formalization preparation cancelled")
        successful = True
    finally:
        try:
            resources.close()
            closed = True
        finally:
            try:
                if claimed:
                    release_workflow_startup_owner(
                        run_id,
                        outcome=(
                            "prepared"
                            if successful and closed and resources.failure is None
                            else (
                                "cancelled"
                                if cancelled.is_set() and closed and resources.failure is None
                                else "failed"
                            )
                        ),
                        metadata={"preflight_resources": resources.evidence()},
                    )
            finally:
                for previous_signum, handler in handlers.items():
                    signal.signal(previous_signum, handler)
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
