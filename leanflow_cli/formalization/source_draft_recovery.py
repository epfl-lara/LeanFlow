"""Recover completed unaccepted drafts locally without reentering paid preparation or authoring."""

from __future__ import annotations

import json
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from core.utils import atomic_json_write
from leanflow_cli.formalization.evidence import contained, identifier
from leanflow_cli.formalization.records import load_document
from leanflow_cli.formalization.source_draft import (
    freeze_preview,
    load_run_preview,
    preview_is_current,
)

LOCAL_RETENTION_SECONDS = 10.0
MAX_DRAFT_STATE_BYTES = 64 * 1024 * 1024


def assert_resume_draft_mode(
    root: Path, environment: dict[str, str], *, explicit: bool, effective: bool
) -> None:
    """Refuse changed explicit or implicit draft semantics before another paid preparation."""
    run_id = environment.get("LEANFLOW_NATIVE_RESUME_RUN_ID", "")
    if not run_id:
        return
    path = contained(root, f".leanflow/native-workflows/{identifier(run_id)}/state.json")
    if not path.exists():
        return
    state = json.loads(read_regular_bytes(path, MAX_DRAFT_STATE_BYTES))
    spent_draft = (
        isinstance(state, dict)
        and state.get("source_draft_only")
        and (
            state.get("source_draft_completed")
            or state.get("source_draft_terminal_observed")
            or state.get("native_status") == "completed"
        )
    )
    if isinstance(state, dict) and state.get("native_cleanup_unverified") and not spent_draft:
        raise ValueError(
            "Prior native process closure is unverified; retain its private inputs and start a separate campaign"
        )
    if not isinstance(state, dict) or state.get("workflow") != "formalize":
        raise ValueError("Resume history is not a formalization campaign")
    if bool(state.get("source_draft_intent", False)) != explicit:
        raise ValueError("Start a new campaign when changing source-draft-only intent")
    if bool(state.get("source_draft_only", False)) != effective:
        raise ValueError("Effective source-draft mode changed; start a new campaign")


def completed_draft_state(
    root: Path, environment: dict[str, str], *, explicit: bool, backend: str
) -> dict[str, Any] | None:
    """Recognize spent author history before source intake, budgets or dependency effects."""
    run_id = environment.get("LEANFLOW_NATIVE_RESUME_RUN_ID", "")
    if not run_id:
        return None
    path = contained(root, f".leanflow/native-workflows/{identifier(run_id)}/state.json")
    if not path.exists():
        return None
    state = json.loads(read_regular_bytes(path, MAX_DRAFT_STATE_BYTES))
    spent_draft = (
        isinstance(state, dict)
        and state.get("source_draft_only")
        and (
            state.get("source_draft_completed")
            or state.get("source_draft_terminal_observed")
            or state.get("native_status") == "completed"
        )
    )
    if isinstance(state, dict) and state.get("native_cleanup_unverified") and not spent_draft:
        raise ValueError(
            "Prior native process closure is unverified; retain its private inputs and start a separate campaign"
        )
    if (
        not isinstance(state, dict)
        or not state.get("source_draft_only")
        or not (
            state.get("source_draft_completed")
            or state.get("source_draft_terminal_observed")
            or state.get("native_status") == "completed"
        )
    ):
        return None
    if (
        state.get("version") != 2
        or state.get("workflow") != "formalize"
        or state.get("backend") != backend
        or state.get("run_id") != run_id
    ):
        raise ValueError("Completed draft history differs from this campaign and provider")
    if bool(state.get("source_draft_intent", False)) != explicit:
        raise ValueError("Start a new campaign when changing source-draft-only intent")
    return state


def retain_completed_draft(
    root: Path,
    directory: Path,
    state: dict[str, Any],
    environment: dict[str, str],
    *,
    cancelled: Callable[[], bool] | None = None,
    recovery: bool = False,
) -> dict[str, Any]:
    """Save or reload this exact run after proven closure, using a separate bounded local deadline."""
    state.pop("draft_preview", None)
    state.pop("draft_preview_attached", None)
    try:
        document_id = state.get("formalization_document_id") or environment.get(
            "LEANFLOW_FORMALIZATION_DOCUMENT_ID", ""
        )
        revision = state.get("formalization_base_revision") or environment.get(
            "LEANFLOW_FORMALIZATION_REVISION", ""
        )
        record = load_document(root, identifier(document_id), identifier(revision))
        run_id = identifier(state["run_id"])
        preview = load_run_preview(root, record, run_id)
        if preview is None:
            if not state.get("source_draft_quiescent"):
                raise ValueError(
                    "Completed author process closure is unverified; retain this run's private project and bridge evidence before recovery"
                )
            if cancelled is not None and cancelled():
                raise ValueError(
                    "Explicit user cancellation deferred local preview retention; resume to retry without another author"
                )
            layout = state.get("source_draft_layout")
            if not isinstance(layout, dict):
                raise ValueError("Completed draft has no persisted checked output allocation")
            preview = freeze_preview(
                root,
                record,
                contained(directory, "project"),
                layout=layout,
                run_id=run_id,
                selection_token=state["formalization_selection_token"],
                model_report=str(state.get("final_response", "")),
                deadline=time.monotonic() + LOCAL_RETENTION_SECONDS,
                cancelled=cancelled,
                recovery=recovery,
            )
        state["draft_preview"] = preview
        state["draft_preview_attached"] = preview_is_current(root, record, preview)
        state.pop("draft_retention_error", None)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        state["draft_retention_error"] = str(error)
    if cancelled is not None and cancelled():
        state["explicit_cancel_requested"] = True
        if not state.get("draft_preview"):
            state.update(
                status="cancelled",
                error="Explicit user cancellation deferred local preview retention; resume to retry without another author",
            )
    if state.get("draft_preview") and (
        state.get("status") in {"cancelled", "running"}
        or "deferred local preview retention" in str(state.get("error", ""))
    ):
        state.update(
            status="blocked",
            error="Unaccepted source draft saved; source uncertainty still blocks translation acceptance, publication and proving",
        )
    state["terminal"] = True
    if state.get("source_draft_completed") or state.get("native_status") == "completed":
        state["source_draft_completed"] = True
    if state.get("status") == "running":
        state.update(
            status="blocked",
            error="The source draft author turn was spent; no second author turn was launched",
        )
    atomic_json_write(contained(directory, "state.json"), state)
    return state


def recover_completed_draft(
    root: Path, directory: Path, state: dict[str, Any], environment: dict[str, str]
) -> dict[str, Any]:
    """Honor fresh user cancellation while restoring bounded local history independently of spent budgets."""
    cancelled = threading.Event()
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: cancelled.set())
    try:
        return retain_completed_draft(
            root, directory, state, environment, cancelled=cancelled.is_set, recovery=True
        )
    finally:
        for previous_signum, handler in previous.items():
            signal.signal(previous_signum, handler)
