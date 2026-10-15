"""Launch one prover controller and publish terminal evidence for startup failures."""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from core.file_copy import copy_tree
from core.utils import atomic_json_write
from leanflow_cli.runtime.project_operations import ProjectBusyError, project_activity
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.observer import RunObserver, exit_code
from leanflow_cli.workflows.prover.source import (
    SourceConflictError,
    import_only_source,
    project_path,
    project_sources,
)
from leanflow_cli.workflows.prover.store import now

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_PATH_FIELDS = frozenset(
    {
        "path",
        "workspace",
        "log_path",
        "scratch_path",
        "candidate_path",
        "plan_path",
        "dag_path",
        "baseline_path",
        "source_path",
        "result_path",
        "report_path",
        "evidence_path",
        "metadata_path",
        "artifacts",
    }
)


def _relocate(value: Any, old: Path, new: Path, key: str = "") -> Any:
    """Remap artifact paths without altering proof text, notes, or source baselines."""
    if isinstance(value, dict):
        return {name: _relocate(item, old, new, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_relocate(item, old, new, key) for item in value]
    if isinstance(value, str) and key in _PATH_FIELDS:
        if value == str(old) or value.startswith(str(old) + "/"):
            return str(new) + value[len(str(old)) :]
    return value


def clone_resume_run(root: Path, previous: str, current: str) -> None:
    """Copy a prior run into new lineage while preserving source text and old evidence."""
    if any(not _IDENTIFIER.fullmatch(value) for value in (previous, current)):
        raise ValueError("invalid resume run ID")
    base = project_path(root, ".leanflow/workflow-state/prover")
    old = project_path(root, str((base / previous).relative_to(root)))
    new = project_path(root, str((base / current).relative_to(root)))
    if not (old / "state.json").is_file() or new.exists():
        raise ValueError("resume source does not exist or destination already exists")
    if any(path.is_symlink() for path in old.rglob("*")):
        raise ValueError("resume artifacts cannot contain symlinks")
    state = json.loads((old / "state.json").read_text())
    if not isinstance(state, dict) or state.get("run_id") != previous:
        raise ValueError("resume snapshot does not match its run ID")
    copy_tree(old, new)
    for path in new.rglob("*.json"):
        try:
            content = json.loads(path.read_text())
        except (ValueError, UnicodeError):
            # Incomplete scratch artifacts are evidence, not required runtime metadata.
            continue
        atomic_json_write(path, _relocate(content, old, new))
    state = _relocate(state, old, new)
    state.update(run_id=current, resumed_from=previous, parent_run_id=previous, terminal=False)
    atomic_json_write(new / "state.json", state)


def _startup_failure(
    root: Path,
    run_id: str,
    error: Exception,
    *,
    config: ProverConfig | None,
    previous: str,
    publish_live: bool,
) -> int:
    """Record a failed execution without resnapshotting source or rewriting prior runs."""
    from leanflow_cli.workflows.prover.runtime import BudgetExhausted

    message = redact_sensitive_text(str(error))[:16000]
    conflict = isinstance(error, SourceConflictError) or "protected source changed" in message
    timed_out = isinstance(error, BudgetExhausted) and error.code == "campaign_wall_time"
    status = (
        "source_conflict"
        if conflict
        else "timeout" if timed_out else "environment_error" if not publish_live else "error"
    )
    guidance = (
        "Compare canonical source with this run's saved baselines and the retained version paths in the error. "
        "The original snapshot was preserved; no source was automatically reaccepted. "
        "A conflicted prover source journal cannot be cleared by restoring canonical bytes for same-run resume. "
        "Preserve its original, candidate, rollback and journal files; reconcile ordinary source versions and start a fresh run. "
        "A pending library configuration promotion also blocks fresh runs until its transaction can recover. "
        "There is currently no automatic prover journal reconciliation command."
        if conflict
        else (
            "The resumed campaign has exhausted its total active-time budget; its prior checkpoint and source are preserved. "
            "Review the saved progress and budget settings before another launch."
            if timed_out
            else "Correct the reported startup problem, then resume the saved run or launch again."
        )
    )
    try:
        directory = project_path(root, f".leanflow/workflow-state/prover/{run_id}")
        directory.mkdir(parents=True, exist_ok=True)
        state_file = project_path(root, str((directory / "state.json").relative_to(root)))
        state: dict[str, Any] = {}
        if state_file.is_file():
            try:
                existing = json.loads(state_file.read_text())
                if isinstance(existing, dict) and existing.get("run_id") in {run_id, previous}:
                    state = existing
            except (ValueError, OSError):
                pass
        state.update(
            version=1,
            run_id=run_id,
            mode=state.get("mode", config.mode if config else ProverConfig().mode),
            phase=status,
            status=status,
            terminal=True,
            error=message,
            next_step=guidance,
            startup_failed=True,
            finished_at=now(),
            updated_at=now(),
        )
        state.setdefault("started_at", now())
        state.setdefault("dag", {"nodes": [], "roots": [], "revision": 0})
        state.setdefault("jobs", [])
        state.setdefault("changes", [])
        state.setdefault("metrics", {"api_calls": 0, "cost_usd": None})
        if previous:
            state.update(resumed_from=previous, parent_run_id=previous)
        if timed_out:
            state["stop_reason"] = {
                "code": "campaign_wall_time",
                "scope": "campaign",
                "message": message,
                "used": state.get("metrics", {}).get("elapsed_s"),
                "limit": config.wall_time_s if config else None,
                "next_step": guidance,
            }
        if config and "config" not in state:
            state["config"] = config.to_mapping()
        atomic_json_write(state_file, state)
        if publish_live:
            observer = RunObserver(root, run_id)
            observer.start(state, resumed=bool(previous))
            observer.finish(state)
    except Exception as publication_error:
        # A broken or symlinked state directory is never an excuse to write elsewhere.
        print(f"Could not persist startup status: {redact_sensitive_text(str(publication_error))}")
    print(f"LeanFlow prover {run_id}: {message}\n{guidance}", flush=True)
    return exit_code(status)


def main() -> int:
    """Launch through the shared provider environment with observable failure outcomes."""
    root = Path(os.environ.get("LEANFLOW_PROJECT_ROOT", os.getcwd())).resolve()
    try:
        with project_activity(root):
            return _run_project(root)
    except ProjectBusyError as error:
        # Setup owns the project: even startup-error publication would be a conflicting write.
        print(f"LeanFlow prover could not start: {error}", flush=True)
        return 1


def _run_project(root: Path) -> int:
    """Hold project activity ownership through controller startup and terminal evidence."""
    from leanflow_cli.workflows.prover.runtime import ProverRuntime

    previous = os.environ.get("LEANFLOW_PROVER_RESUME_RUN_ID", "").strip()
    requested_id = os.environ.get("LEANFLOW_WORKFLOW_RUN_ID", "").strip()
    run_id = requested_id if _IDENTIFIER.fullmatch(requested_id) else uuid.uuid4().hex[:16]
    if run_id == previous:
        run_id = uuid.uuid4().hex[:16]
    config: ProverConfig | None = None
    publish_live = True
    try:
        from leanflow_cli.formalization.publication import (
            assert_no_pending_publication,
            recover_pending_publication,
        )

        recover_pending_publication(root)
        assert_no_pending_publication(root)
        if requested_id and not _IDENTIFIER.fullmatch(requested_id):
            raise ValueError("invalid execution run ID")
        existing = project_path(root, f".leanflow/workflow-state/prover/{run_id}")
        if existing.exists():
            run_id = uuid.uuid4().hex[:16]
            raise ValueError("execution run ID already exists; previous evidence was preserved")
        config = ProverConfig.from_env()
        active = os.environ.get("LEANFLOW_NATIVE_ACTIVE_FILE", "").strip()
        targets = [root / active] if active else project_sources(root)
        handoff: dict[str, Any] | None = None
        handoff_path = os.environ.get("LEANFLOW_FORMALIZATION_HANDOFF", "").strip()
        handoff_revision = os.environ.get("LEANFLOW_FORMALIZATION_REVISION", "").strip()
        if bool(handoff_path) != bool(handoff_revision):
            raise ValueError("Formalization proving requires both a handoff and its exact revision")
        if handoff_path:
            from leanflow_cli.formalization.handoff import authorized_holes, validate_handoff

            path = Path(handoff_path)
            handoff = validate_handoff(
                root,
                path if path.is_absolute() else root / path,
                handoff_revision,
                check_sources=not bool(previous),
            )
            reviewed_axioms = handoff.get("acceptance_policy", {}).get("allowed_axioms")
            if not isinstance(reviewed_axioms, list) or set(reviewed_axioms) != set(
                config.allowed_axioms
            ):
                raise ValueError(
                    "Prover axiom policy differs from the reviewed formalization policy"
                )
            targets = [project_path(root, relative) for relative in authorized_holes(handoff)]
            if active and {root / active} != set(targets):
                raise ValueError(
                    "An active file cannot override the exact formalization proof scope"
                )
            if not targets:
                state = {
                    "version": 2,
                    "run_id": run_id,
                    "mode": config.mode,
                    "status": "no_proof_obligations",
                    "phase": "no_proof_obligations",
                    "terminal": True,
                    "started_at": now(),
                    "finished_at": now(),
                    "formalization_handoff": handoff,
                    "targets": [],
                    "dag": {"nodes": [], "roots": [], "revision": 0},
                    "metrics": {"api_calls": 0, "cost_usd": None},
                    "config": config.to_mapping(),
                }
                atomic_json_write(existing / "state.json", state)
                observer = RunObserver(root, run_id)
                observer.start(state)
                observer.finish(state)
                print(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "status": "no_proof_obligations",
                            "metrics": state["metrics"],
                        }
                    )
                )
                return 0
        lock_path = project_path(root, ".leanflow/workflow-state/prover/controller.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                publish_live = False
                raise RuntimeError("A prover controller already owns this project") from exc
            if previous:
                clone_resume_run(root, previous, run_id)
                snapshot = json.loads(
                    (root / f".leanflow/workflow-state/prover/{run_id}/state.json").read_text()
                )
                saved_handoff = snapshot.get("formalization_handoff")
                if saved_handoff and handoff is None:
                    from leanflow_cli.formalization.handoff import validate_handoff
                    from leanflow_cli.formalization.records import document_directory

                    handoff = validate_handoff(
                        root,
                        document_directory(root, saved_handoff["document_id"], published=True)
                        / "handoff.json",
                        saved_handoff["revision_id"],
                        check_sources=False,
                    )
                if saved_handoff != handoff:
                    raise ValueError("Resume formalization handoff differs from the saved run")
                saved_targets = [project_path(root, path) for path in snapshot["targets"]]
                if active and {path.resolve() for path in targets} != set(saved_targets):
                    raise ValueError(
                        "resume target scope differs from the saved run; launch a new run to change targets"
                    )
                targets = saved_targets
            from leanflow_cli.formalization.publication import publication_guard

            with publication_guard(root):
                runtime = ProverRuntime(
                    root=root,
                    targets=targets,
                    config=config,
                    run_id=run_id,
                    resume=bool(previous),
                    goal=os.environ.get("LEANFLOW_NATIVE_EXPLICIT_GOAL", ""),
                    observer=RunObserver(root, run_id),
                    formalization_handoff=handoff,
                )
            if (
                active
                and not runtime.dag.roots
                and not previous
                and import_only_source(
                    runtime.documents[str(targets[0].resolve().relative_to(root))].baseline
                )
            ):
                raise ValueError(
                    f"No literal sorry goals were found in {active}. Imported files are outside "
                    "this target's proof scope. Choose the file containing sorry, or leave Target "
                    "empty to scan the whole project. No agent was started."
                )
            print(f"LeanFlow {runtime.config.mode} prover: {run_id}", flush=True)
            print(f"Plan and progress: {runtime.store.directory}", flush=True)
            result = runtime.run()
            print(
                json.dumps(
                    {"run_id": run_id, "status": result["status"], "metrics": result["metrics"]},
                    indent=2,
                )
            )
            return exit_code(result["status"])
    except Exception as error:
        return _startup_failure(
            root, run_id, error, config=config, previous=previous, publish_live=publish_live
        )
