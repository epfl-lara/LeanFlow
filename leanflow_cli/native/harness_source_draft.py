"""Persist completed source-author and bridge closure boundaries before local preview retention."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent.runtime.harness.models import NativeCleanupUnverified
from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.native_bridge import NativeBridge


def draft_reported_blocker(response: str, fallback: str) -> str:
    """Preserve an author's explicit blocker while retaining independent source diagnostics."""
    try:
        reported = json.loads(response)
    except (ValueError, TypeError):
        return fallback
    if isinstance(reported, dict) and reported.get("status") == "blocked":
        return str(reported.get("reason", "External blocker"))
    return fallback


def observe_draft_terminal(
    backend: str, event: dict[str, Any], state: dict[str, Any], state_path: Path
) -> None:
    """Spend the author turn on successful native completion without inferring process closure."""
    completed = (
        event.get("type") == "result"
        and not bool(event.get("is_error"))
        and event.get("subtype", "success") == "success"
        if backend == "claude"
        else event.get("type") == "turn.completed"
    )
    if completed:
        state.update(source_draft_terminal_observed=True, source_draft_quiescent=False)
        atomic_json_write(state_path, state)


def drain_author_bridge(
    bridge: NativeBridge, state: dict[str, Any], state_path: Path, *, draft_only: bool
) -> None:
    """Retire author tools before clearing normal-run liveness ahead of independent checking."""
    try:
        bridge.drain()
        if state.get("native_process_cleanup_verified") is True and not draft_only:
            from leanflow_cli.workflows.prover.check_process import close_check_workers

            close_check_workers(bridge.toolset.workspace)
            state.update(
                native_cleanup_unverified=bool(state.get("independent_cleanup_unverified")),
                native_tool_cleanup_unverified=False,
            )
            atomic_json_write(state_path, state)
    except BaseException:
        _retain_uncertain_tools(state, state_path)
        raise


def _retain_uncertain_tools(state: dict[str, Any], state_path: Path) -> None:
    """Preserve invocation inputs when a checker or bridge close cannot finish."""
    state.update(native_cleanup_unverified=True, native_tool_cleanup_unverified=True)
    if state.get("source_draft_only"):
        state["source_draft_quiescent"] = False
    atomic_json_write(state_path, state)


@contextmanager
def closed_draft_bridge(
    bridge: NativeBridge, state: dict[str, Any], state_path: Path, *, draft_only: bool
) -> Iterator[NativeBridge]:
    """Record quiescence only after bridge operations and owned checker cleanup returned."""
    active = bridge.__enter__()
    try:
        yield active
    finally:
        try:
            bridge.__exit__(*sys.exc_info())
            if not bridge.closed:
                raise NativeCleanupUnverified(
                    "Author bridge closure is unverified; retain its inputs"
                )
        except BaseException:
            _retain_uncertain_tools(state, state_path)
            raise
        state["native_tool_cleanup_unverified"] = False
        if state.get("native_process_cleanup_verified") is True:
            state["native_cleanup_unverified"] = bool(state.get("independent_cleanup_unverified"))
        if draft_only and state.get("source_draft_native_closed"):
            state["source_draft_quiescent"] = True
        atomic_json_write(state_path, state)
