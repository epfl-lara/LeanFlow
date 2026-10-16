"""Prepare source-backed launches under cancellable ownership before native-controller handoff."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from core.campaign_limits import CampaignLimits
from core.deadlines import deadline_after
from leanflow_cli.config import load_config
from leanflow_cli.formalization.formalization_documents import _select_formalization_document
from leanflow_cli.formalization.formalization_models import FormalizationDocumentError
from leanflow_cli.formalization.intake import author_blocking_diagnostics, draft_source_rejection
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.preflight_owner import PreflightProgress
from leanflow_cli.formalization.preparation import (
    assert_preparation_current,
    load_preparation_receipt,
    prepare_document_project,
)
from leanflow_cli.formalization.preparation_campaign import (
    campaign_budget,
    campaign_lock,
    select_campaign,
)
from leanflow_cli.formalization.preparation_discovery import discover_preparation_plan
from leanflow_cli.formalization.preparation_plan import default_preparation_plan
from leanflow_cli.formalization.preparation_transaction import assert_no_pending_preparation
from leanflow_cli.formalization.records import (
    assert_author_selection,
    create_document,
    current_selection_token,
)
from leanflow_cli.formalization.source_draft import assert_unaccepted_draft
from leanflow_cli.formalization.source_draft_recovery import (
    assert_resume_draft_mode,
    completed_draft_state,
)
from leanflow_cli.runtime.project_operations import project_activity
from leanflow_cli.workflows.project import (
    LeanFlowProject,
    ProjectNotFoundError,
    discover_leanflow_project,
    find_lean_project_root,
)
from leanflow_cli.workflows.project_setup.process import safe_text
from leanflow_cli.workflows.project_setup.state import inspect_project


def _report_preparation_progress(event: dict[str, Any]) -> None:
    """Show automatic setup phases on stderr without flooding terminals with protocol output."""
    if event.get("type") == "output":
        return
    phase = safe_text(str(event.get("phase", "setup")))[:120]
    message = safe_text(str(event.get("message", "")))[:2000]
    print(f"[Formalization setup: {phase}] {message}", file=sys.stderr, flush=True)
    if event.get("log_path"):
        print(f"Log: {safe_text(str(event['log_path']))[:2000]}", file=sys.stderr, flush=True)


def source_project_root(cwd: Path) -> Path:
    """Retain the nearest existing Lake root or the user's selected source folder."""
    return find_lean_project_root(cwd) or cwd


def _require_project_baseline(root: Path) -> None:
    """Require checked tools before planning, retaining stronger current preparation authority."""
    assert_no_pending_preparation(root)
    try:
        setup = inspect_project(root)
        if setup.get("ready") is True and setup.get("configured") is True:
            return
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    try:
        assert_preparation_current(root)
        return
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
        command = shlex.join(["leanflow", "project", "setup", str(root), "--preset", "mathlib"])
        raise FormalizationDocumentError(
            "Set up project tools before formalization planning. "
            "In the VS Code Formalization tab, click Set up project, or run "
            f"`{command}`. Setup checks Lean, installed dependencies and the REPL; "
            "formalization can then estimate and add missing dependencies in its checked transaction."
        ) from None


def preview_source_project(cwd: Path, *, historical: bool = False) -> LeanFlowProject:
    """Represent a source-only preview without creating manifests, inventory, or dependencies."""
    if not historical:
        try:
            return discover_leanflow_project(cwd)
        except ProjectNotFoundError:
            pass
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
    source_draft_only: bool = False,
) -> dict[str, Any]:
    """Expose cancellation and aggregate resource ownership before paid preparation."""
    env = dict(os.environ if environment is None else environment)
    root = source_project_root(cwd)
    completed = completed_draft_state(
        root, env, explicit=source_draft_only, backend=str(runtime["provider"])
    )
    if completed is not None:
        return {"root": root, "completed_source_draft": completed}
    run_id = (
        env.get("LEANFLOW_NATIVE_RESUME_RUN_ID")
        or env.get("LEANFLOW_WORKFLOW_RUN_ID")
        or ("formalize-cli-" + uuid.uuid4().hex)
    )
    env["LEANFLOW_WORKFLOW_RUN_ID"] = run_id
    from leanflow_cli.formalization.preflight_owner import preflight_owner

    with (
        ExitStack() as accounting_stack,
        preflight_owner(root, env) as (cancelled, resources, progress),
    ):

        def claim_accounting() -> None:
            """Retain serial accounting through final checkpoint worker closure."""
            accounting = accounting_stack.enter_context(campaign_lock(root, run_id))
            progress.bind_accounting_owner(accounting)

        def check() -> None:
            """Stop preparation when either resource or live ownership inspection fails."""
            progress.check()
            resources.check(force=True)

        result = _prepare_formalization_launch(
            cwd,
            workflow_args,
            runtime,
            environment=env,
            user_goal=user_goal,
            source_draft_only=source_draft_only,
            cancelled=cancelled,
            check=check,
            progress=progress,
            claim_accounting=claim_accounting,
        )
        return {**result, "run_id": run_id}


def _prepare_formalization_launch(
    cwd: Path,
    workflow_args: str,
    runtime: dict[str, Any],
    *,
    environment: dict[str, str] | None = None,
    user_goal: str = "",
    source_draft_only: bool = False,
    cancelled: threading.Event,
    check: Callable[[], None],
    progress: PreflightProgress,
    claim_accounting: Callable[[], None],
) -> dict[str, Any]:
    """Own one source preparation campaign and return its checked infrastructure and shared budget."""
    root = source_project_root(cwd)
    env = dict(os.environ if environment is None else environment)
    completed = completed_draft_state(
        root, env, explicit=source_draft_only, backend=str(runtime["provider"])
    )
    if completed is not None:
        return {"root": root, "completed_source_draft": completed}
    progress.phase({"phase": "checking_baseline"})
    check()
    _require_project_baseline(root)
    check()
    selected = _select_formalization_document(root, cwd, workflow_args)
    with project_activity(root):
        record = create_document(root, selected.source_path)
        assert_author_selection(
            record,
            env.get("LEANFLOW_NATIVE_RESUME_RUN_ID") or env.get("LEANFLOW_WORKFLOW_RUN_ID", ""),
            current_selection_token(root, record["document_id"]),
        )
        inventory = complete_inventory(record)
        effective_draft = (
            draft_source_rejection(inventory, source_draft_only=source_draft_only) is not None
        )
        assert_resume_draft_mode(root, env, explicit=source_draft_only, effective=effective_draft)
        if effective_draft:
            assert_unaccepted_draft(record)
        blockers = author_blocking_diagnostics(inventory, source_draft_only=source_draft_only)
        if blockers:
            raise FormalizationDocumentError(
                "Source extraction needs repair before project preparation: "
                + "; ".join(row["message"] for row in blockers)
            )
    check()
    if cancelled.is_set():
        raise ValueError("Formalization preparation cancelled before planning")
    limits = load_config().get("limits", {})
    configured = {
        name: env.get("LEANFLOW_PROVER_" + name.upper(), limits.get(name))
        for name in ("agent_calls", "tokens", "wall_time_s")
    }
    claim_accounting()
    try:
        previous_intent = load_preparation_receipt(root)
    except (OSError, ValueError, RuntimeError):
        previous_intent = None
    binding = select_campaign(
        root,
        record,
        run_id=env["LEANFLOW_WORKFLOW_RUN_ID"],
        limits=configured,
        backend=str(runtime["provider"]),
        selection_token=current_selection_token(root, record["document_id"]),
        source_draft_only=source_draft_only,
        effective_draft=effective_draft,
        resume=bool(env.get("LEANFLOW_NATIVE_RESUME_RUN_ID")),
    )
    budget_path = campaign_budget(root, binding)
    directory = budget_path.parent
    budget = CampaignLimits(budget_path, binding["limits"])
    progress.bind_budget(budget)
    # Infrastructure freshness cannot select, reset, or authorize accounting.
    try:
        previous = assert_preparation_current(root)
    except (OSError, ValueError, RuntimeError):
        previous = None
    if (
        previous
        and env.get("LEANFLOW_NATIVE_RESUME_RUN_ID")
        and previous.get("source_bundle") == record["source_bundle"]
        and previous.get("budget_path") == budget_path.relative_to(root).as_posix()
    ):
        return {"root": root, "receipt": previous, "budget_path": budget_path}

    def report(event: dict[str, Any]) -> None:
        """Keep terminal messages and exact-owner progress on the same preparation phase."""
        _report_preparation_progress(event)
        progress.phase(event)

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
    check()
    if cancelled.is_set():
        raise ValueError("Formalization preparation cancelled before discovery")
    if discovery_enabled:
        report(
            {
                "phase": "dependency_plan",
                "message": "Estimating mathematical dependencies with the selected native backend…",
            }
        )
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
            on_event=progress.native_event,
        )
        check()
        if discovered.get("accepted") is not True:
            discovery_failure = ValueError(
                "Formalization preparation discovery needs correction: "
                + str(
                    discovered.get(
                        "error",
                        discovered.get("unauthorized_packages") or discovered.get("status"),
                    )
                )
            )
            progress.failed(
                discovery_failure,
                code=str(discovered.get("status") or "preparation_discovery_error"),
            )
            raise discovery_failure
        plan = discovered["plan"]
    remaining = budget.remaining()
    if cancelled.is_set() or (remaining is not None and remaining <= 0):
        raise ValueError("Formalization preparation campaign was cancelled or exhausted")
    report(
        {
            "phase": "project_setup",
            "message": "Preparing and checking pinned dependencies in a private project…",
        }
    )
    prepared = prepare_document_project(
        root,
        record,
        plan=plan,
        progress=report,
        cancel=cancelled,
        timeout_s=min(1800.0, remaining) if remaining is not None else 1800.0,
        budget_path=budget_path,
        network_allowed=env.get("LEANFLOW_FORMALIZATION_ALLOW_NETWORK", "true").lower()
        not in {"0", "false", "off"},
    )
    check()
    if prepared.get("accepted") is not True:
        setup_failure = ValueError(
            "Formalization project preparation failed: "
            + str(prepared.get("error", prepared.get("status")))
        )
        progress.failed(
            setup_failure,
            code=str(
                prepared.get("error_code") or prepared.get("status") or "project_setup_failed"
            ),
        )
        raise setup_failure
    report({"phase": "ready", "message": "Project setup passed; continuing to formalization."})
    return {"root": root, "receipt": prepared["receipt"], "budget_path": budget_path}
