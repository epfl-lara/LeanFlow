"""Prepare source-only formalization launches before project discovery or active workflow ownership."""

from __future__ import annotations

import os
import signal
import threading
import uuid
from pathlib import Path
from typing import Any

from core.campaign_limits import CampaignLimits
from core.deadlines import deadline_after
from leanflow_cli.config import load_config
from leanflow_cli.formalization.evidence import contained
from leanflow_cli.formalization.formalization_documents import _select_formalization_document
from leanflow_cli.formalization.preparation import (
    assert_preparation_current,
    load_preparation_receipt,
    prepare_document_project,
)
from leanflow_cli.formalization.preparation_discovery import discover_preparation_plan
from leanflow_cli.formalization.preparation_plan import default_preparation_plan
from leanflow_cli.formalization.records import create_document
from leanflow_cli.runtime.project_operations import project_activity
from leanflow_cli.workflows.project import (
    LeanFlowProject,
    ProjectNotFoundError,
    discover_leanflow_project,
    find_lean_project_root,
)


def source_project_root(cwd: Path) -> Path:
    """Retain the nearest existing Lake root or the user's selected source folder."""
    return find_lean_project_root(cwd) or cwd


def preview_source_project(cwd: Path) -> LeanFlowProject:
    """Represent a source-only preview without creating manifests, inventory, or dependencies."""
    try:
        return discover_leanflow_project(cwd)
    except ProjectNotFoundError:
        root = source_project_root(cwd)
        state = root / ".leanflow"
        return LeanFlowProject(
            root=root,
            leanflow_dir=state,
            manifest_path=state / "project.yaml",
            name=root.name,
            kind="source_only",
            schema_version=1,
            lean_root=root,
            runtime_dir=state / "runtime",
            cache_dir=state / "cache",
            workflows_dir=state / "workflows",
            created_at="",
            source_mode="source_only",
            template_source="",
            blueprint_markers=(),
            manifest={},
        )


def prepare_formalization_launch(
    cwd: Path,
    workflow_args: str,
    runtime: dict[str, Any],
    *,
    environment: dict[str, str] | None = None,
    user_goal: str = "",
) -> dict[str, Any]:
    """Own one source preparation campaign and return its checked infrastructure and shared budget."""
    root = source_project_root(cwd)
    selected = _select_formalization_document(root, cwd, workflow_args)
    with project_activity(root):
        record = create_document(root, selected.source_path)
    env = dict(os.environ if environment is None else environment)
    limits = load_config().get("limits", {})
    configured = {
        name: env.get("LEANFLOW_PROVER_" + name.upper(), limits.get(name))
        for name in ("agent_calls", "tokens", "wall_time_s")
    }
    try:
        previous_intent = load_preparation_receipt(root)
    except (OSError, ValueError, RuntimeError):
        previous_intent = None
    # An unchanged prepared document can reuse its exact infrastructure and slice
    # while an explicit resume keeps cumulative campaign usage.
    try:
        previous = assert_preparation_current(root)
    except (OSError, ValueError, RuntimeError):
        previous = None
    if (
        previous
        and env.get("LEANFLOW_NATIVE_RESUME_RUN_ID")
        and previous.get("source_bundle") == record["source_bundle"]
        and previous.get("budget_path")
    ):
        path = Path(previous["budget_path"])
        path = root / path if not path.is_absolute() else path
        path = contained(root, path.relative_to(root).as_posix())
        if path.is_file():
            try:
                budget = CampaignLimits(path, configured)
            except ValueError as error:
                if "Start a new campaign when changing limits" not in str(error):
                    raise
            else:
                budget.persist()
                return {"root": root, "receipt": previous, "budget_path": path}
    directory = contained(root, ".leanflow/formalization-campaigns/" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    budget_path = directory / "budget.json"
    budget = CampaignLimits(budget_path, configured)
    cancelled = threading.Event()
    previous_signals: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signals[signum] = signal.signal(signum, lambda *_: cancelled.set())
    try:
        plan = default_preparation_plan(
            root,
            record,
            preset=env.get(
                "LEANFLOW_FORMALIZATION_PRESET",
                (previous_intent or {}).get("plan", {}).get("preset", "lean"),
            ),
        )
        discovery_enabled = env.get("LEANFLOW_FORMALIZATION_DISCOVERY", "true").lower() not in {
            "0",
            "false",
            "off",
        }
        if discovery_enabled:
            discovered = discover_preparation_plan(
                root,
                record,
                plan,
                backend=str(runtime["provider"]),
                model=str(runtime.get("model") or ""),
                effort=str(runtime.get("reasoning_effort") or ""),
                environment={**env, "LEANFLOW_PROJECT_ROOT": str(root)},
                budget=budget,
                directory=directory / "discovery",
                deadline=deadline_after(budget.remaining()),
                cancelled=cancelled.is_set,
                user_goal=user_goal,
            )
            if discovered.get("accepted") is not True:
                raise ValueError(
                    "Formalization preparation discovery needs correction: "
                    + str(
                        discovered.get(
                            "error",
                            discovered.get("unauthorized_packages") or discovered.get("status"),
                        )
                    )
                )
            plan = discovered["plan"]
        remaining = budget.remaining()
        if cancelled.is_set() or (remaining is not None and remaining <= 0):
            raise ValueError("Formalization preparation campaign was cancelled or exhausted")
        prepared = prepare_document_project(
            root,
            record,
            plan=plan,
            cancel=cancelled,
            timeout_s=min(1800.0, remaining) if remaining is not None else 1800.0,
            budget_path=budget_path,
            network_allowed=env.get("LEANFLOW_FORMALIZATION_ALLOW_NETWORK", "true").lower()
            not in {"0", "false", "off"},
        )
        if prepared.get("accepted") is not True:
            raise ValueError(
                "Formalization project preparation failed: "
                + str(prepared.get("error", prepared.get("status")))
            )
        return {"root": root, "receipt": prepared["receipt"], "budget_path": budget_path}
    finally:
        budget.persist()
        for previous_signum, handler in previous_signals.items():
            signal.signal(previous_signum, handler)
