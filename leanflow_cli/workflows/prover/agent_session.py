"""Delegate one complete prover conversation to a configured native agent runtime."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from agent.runtime.harness.network_policy import restricted_research
from agent.runtime.harness.observation_stream import NativeObservationStream
from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.native_snapshot import (
    prepare_snapshot,
    release_snapshot_dependencies,
    remap_project_text,
)
from leanflow_cli.workflows.prover.research_policy import ONLINE_POLICY, RESTRICTED_POLICY
from leanflow_cli.workflows.prover.session_guidance import GuidanceInbox, skill_guidance
from leanflow_cli.workflows.prover.session_tools import SessionTools
from leanflow_cli.workflows.prover.store import now

EventCallback = Callable[[str, dict[str, Any]], None]

_CONTRACT = """You are working in a private LeanFlow project copy.
Your runtime owns model settings, tool use, conversation history and compaction.
Use the LeanFlow bridge get_assignment tool to recover the immutable assignment after compaction.
Check get_guidance for new user instructions; operation polls also return addressed guidance.
Canonical project files and controller records are outside your authority. Only the
controller can accept a proof, publish a report, or promote changes. Preserve the
supplied declarations and replace only the assigned literal sorry holes.
Use the LeanFlow bridge submit_candidate tool with JSON proof/proofs replacements, or submit_report
with the requested report JSON. These return operation IDs: poll_operation retrieves
independent controller feedback. A rejected submission is feedback to continue this
same conversation. Acceptance is established only by an accepted controller receipt.
If the proof needs an additional installed Lean module, include an imports list
in the submission JSON. The controller checks protected declaration types before
adding these imports with an accepted proof; scratch imports are also retained.
Use start_lean_check and start_command with poll_operation for long Lean checks,
builds, and commands. Polling never cancels
an operation, and there is no LeanFlow per-response or per-tool time budget.
Stop when the task is accepted, an actual blocker requires user input, or the global
controller cancels the job. Return the requested final JSON report.
"""

_RESTRICTED_CONTRACT = """This is a local-only research session. Use only the supplied
LeanFlow bridge tools; direct native tools and external research are disabled.
The bridge's commands cannot access the network, including local services.
Use installed Lean libraries and the supplied problem files to develop the proof.
Do not look up this problem's solution or attempt to bypass these restrictions.
"""


def _map_paths(value: Any, project: Path, workspace: Path, snapshot: Path) -> Any:
    """Replace canonical paths with the agent's private project and scratch paths."""
    if isinstance(value, str):
        return remap_project_text(
            value, project, snapshot, replacements={workspace: snapshot / ".leanflow-job"}
        )
    if isinstance(value, list):
        return [_map_paths(item, project, workspace, snapshot) for item in value]
    if isinstance(value, dict):
        return {key: _map_paths(item, project, workspace, snapshot) for key, item in value.items()}
    return value


def run_session(
    *,
    role: str,
    prompt: str,
    project_root: Path,
    workspace: Path,
    config: Mapping[str, Any],
    api_budget: int,
    log_path: Path,
    context: dict[str, Any],
    on_event: EventCallback | None = None,
) -> dict[str, Any]:
    """Invoke one CLI process with job-bound tools and independent proof acceptance."""
    from agent.runtime.harness import HarnessRequest, build_runtime_isolation, run_harness

    restricted = restricted_research(allow_internet=config.get("allow_internet"))
    research_policy = RESTRICTED_POLICY if restricted else ONLINE_POLICY
    runtime_dir = workspace.parent / ".runtime" / workspace.name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = runtime_dir / "request-count.json"
    previous: dict[str, Any] = (
        json.loads(ledger_path.read_text()) if ledger_path.exists() else {"used": 0}
    )
    backend = str(config.get("provider") or os.getenv("LEANFLOW_NATIVE_PROVIDER") or "codex")
    policy_changed = previous.get("research_policy") != research_policy
    registry = config.get("_bridge_registry")
    bridge = registry.get(str(workspace)) if isinstance(registry, dict) else None
    if isinstance(registry, dict) and bridge is not None and policy_changed:
        bridge.close()
        registry.pop(str(workspace), None)
        bridge = None
    snapshot = workspace / "project"
    if restricted and policy_changed and snapshot.exists():
        # A fresh conversation cannot forget research already saved in the project copy.
        error = (
            "This job's existing files were created under a different or unknown research "
            "policy. Start a new run to use clean room or offline mode; the previous "
            "workspace was preserved and no agent was started."
        )
        return {
            "status": "environment_error",
            "final_response": error,
            "error": error,
            "api_calls": int(previous.get("used", 0)),
            "agent_calls": int(previous.get("used", 0)),
            "usage_known": False,
        }
    if previous.get("backend") != backend or policy_changed:
        # Native session IDs and cumulative token baselines belong to one CLI.
        # Legacy ledgers have no provable policy; retain spending but start fresh.
        previous.pop("session_id", None)
        previous.pop("usage_total", None)
    previous.update(backend=backend, research_policy=research_policy)
    # Mark provenance before creating files so a failed native launch can retry safely.
    atomic_json_write(ledger_path, previous)
    previous_calls = int(previous.get("used", 0))
    from agent.runtime.harness.redaction import environment_secrets, redact_native_value

    secrets = environment_secrets(os.environ)
    event_lock = threading.RLock()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(kind: str, details: dict[str, Any]) -> None:
        """Retain authoritative controller evidence separately from native observations."""
        details = redact_native_value(details, secrets)
        evidence_id = uuid.uuid4().hex[:12]
        event = {
            "type": kind,
            "timestamp": now(),
            "agent_id": context.get("job_id", role),
            "run_id": context.get("run_id", ""),
            "evidence_id": evidence_id,
            "details": details,
        }
        with event_lock, log_path.open("a", encoding="utf-8") as stream:
            stream.write(
                redact_sensitive_text(json.dumps(event, ensure_ascii=False, default=str)) + "\n"
            )
        if on_event is not None:
            on_event(kind, {**details, "evidence_id": evidence_id})

    observations = NativeObservationStream(backend)

    def native_event(event: dict[str, Any]) -> None:
        """Checkpoint native identity and publish observational output separately."""
        for kind, details in observations.consume(event):
            emit(kind, details)
        session_id = (
            event.get("thread_id")
            if event.get("type") == "thread.started"
            else event.get("session_id")
        )
        if isinstance(session_id, str) and session_id:
            with event_lock:
                previous["session_id"] = session_id
                atomic_json_write(ledger_path, previous)

    fresh_snapshot = not snapshot.exists() and not snapshot.is_symlink()
    if context.get("formalization_proof_scope"):
        excluded_build_dir = context.get("formalization_build_dir")
        if not isinstance(excluded_build_dir, str) or not excluded_build_dir:
            raise ValueError("Authenticated proof imports have no evaluated build directory")
        prepare_snapshot(project_root, snapshot, excluded_build_dir=excluded_build_dir)
    else:
        prepare_snapshot(project_root, snapshot)
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir(exist_ok=True)
    for name in ("Scratch.lean", "PLAN_job.md", "candidate.txt"):
        source = workspace / name
        if source.is_file() and not source.is_symlink() and not (scratch / name).exists():
            shutil.copy2(source, scratch / name)
    assignment = _map_paths(context, project_root, workspace, snapshot)
    inbox = GuidanceInbox(project_root, runtime_dir, context, role)
    inbox.poll()
    assignment["guidance"] = inbox.contract()

    def guidance() -> dict[str, Any]:
        """Serialize inbox cursor updates across concurrent native tool requests."""
        with event_lock:
            return {"messages": inbox.poll(), "retained_guidance": inbox.contract()}

    remaining = config.get("wall_time_s")
    deadline = None if remaining is None else time.monotonic() + max(0.0, float(remaining))
    cancelled = config.get("_cancelled")
    cancelled = cancelled if callable(cancelled) else None
    toolset = SessionTools(
        role=role,
        project_root=snapshot,
        workspace=scratch,
        context=assignment,
        timeout_s=None,
        deadline=deadline,
        cancelled=cancelled,
        allow_internet=not restricted,
        research_job=config.get("_research_job") if callable(config.get("_research_job")) else None,
    )
    if role in {"prover", "negation"}:
        from leanflow_cli.workflows.prover.check_process import close_check_workers
        from leanflow_cli.workflows.prover.session_setup import check_session_setup

        setup = check_session_setup(toolset, runtime_dir, emit, cache_fresh_snapshot=fresh_snapshot)
        if not setup["accepted"]:
            error = "Isolated prover workspace failed its LeanProbe setup check: " + str(
                setup.get("error") or setup.get("error_code") or setup
            )
            setup_output = {
                "status": (
                    "interrupted"
                    if setup.get("error_code") == "check_cancelled"
                    else "timeout" if setup.get("timed_out") is True else "environment_error"
                ),
                "error": error,
                "final_response": error,
                "api_calls": previous_calls,
                "agent_calls": previous_calls,
                "usage_known": False,
                "setup_check": setup,
            }
            emit("job-session-end", setup_output)
            close_check_workers(scratch)
            try:
                release_snapshot_dependencies(snapshot)
            except (OSError, ValueError) as exc:
                emit("job-cache-cleanup-error", {"error": str(exc)})
            return setup_output
    callbacks = {
        public: config[private]
        for public, private in (
            ("submit_candidate", "_candidate_feedback"),
            ("submit_report", "_report_feedback"),
            ("research", "_research_job"),
        )
        if callable(config.get(private))
    }
    executable = ""
    readable_roots = [Path(__file__).resolve().parents[3]]
    if backend == "lemex":
        from leanflow_cli.runtime.lemex_install import (
            managed_distribution_root,
            resolve_lemex_executable,
        )

        resolved = resolve_lemex_executable()
        executable = str(resolved) if resolved is not None else ""
        managed = managed_distribution_root()
        if managed is not None:
            readable_roots.append(managed)
    started = False

    def admitting() -> None:
        """Journal admission before launch; an unknown launch outcome remains charged."""
        previous.update(
            limit=None, used=previous_calls + 1, unit="agent_invocations", admission="pending"
        )
        atomic_json_write(ledger_path, previous)

    def admitted() -> None:
        """Mark successful process launch and publish exactly one agent invocation."""
        nonlocal started
        started = True
        previous.update(
            limit=None, used=previous_calls + 1, unit="agent_invocations", admission="started"
        )
        atomic_json_write(ledger_path, previous)
        emit(
            "api-request",
            {
                "api_calls": previous_calls + 1,
                "agent_calls": previous_calls + 1,
                "provider": backend,
                "model": str(config.get("model") or ""),
                "reasoning_effort": str(config.get("reasoning_effort") or ""),
            },
        )

    if bridge is None:
        bridge = NativeBridge(
            toolset=toolset,
            context=assignment,
            callbacks=callbacks,
            directory=runtime_dir / "operations",
            on_event=emit,
            persistent=isinstance(registry, dict),
            guidance=guidance,
        )
        if isinstance(registry, dict):
            registry[str(workspace)] = bridge
    with bridge:
        mcp_path = scratch / "mcp-config.json"
        atomic_json_write(mcp_path, bridge.mcp_config())
        policy = build_runtime_isolation(
            backend,
            snapshot,
            denied_roots=(project_root,),
            readable_roots=readable_roots,
        )
        if policy.native_database_root is not None and not restricted:
            database_home = str(policy.native_database_root)
            if previous.get("database_home") != database_home:
                # A migrated database cannot resume an old thread's native history.
                # Controller progress and consumed calls remain authoritative.
                previous.pop("session_id", None)
                previous.pop("usage_total", None)
            previous["database_home"] = database_home
            atomic_json_write(ledger_path, previous)
        result = run_harness(
            HarnessRequest(
                backend=backend,
                prompt=_CONTRACT
                + "\n"
                + (_RESTRICTED_CONTRACT if restricted else "")
                + skill_guidance(project_root, role)
                + "\n"
                + _map_paths(prompt, project_root, workspace, snapshot)
                + "\nASSIGNMENT\n"
                + json.dumps(assignment, ensure_ascii=False),
                cwd=snapshot,
                model=str(config.get("model") or ""),
                effort=str(config.get("reasoning_effort") or ""),
                session_id="" if restricted else str(previous.get("session_id") or ""),
                deadline=deadline,
                cancelled=cancelled,
                mcp_config=mcp_path,
                log_path=runtime_dir / "harness.jsonl",
                previous_usage=None if restricted else previous.get("usage_total"),
                isolation=policy,
                restricted_tools=restricted,
                on_admitting=admitting,
                on_started=admitted,
                executable=executable,
            ),
            on_event=native_event,
        )
        for kind, details in observations.flush():
            emit(kind, details)
        bridge.drain()
        response = bridge.submitted_response or result.final_response
        # Only scratch outputs used by the independent controller cross the boundary.
        for name in ("Scratch.lean", "PLAN_job.md", "candidate.txt"):
            source = scratch / name
            if source.is_file() and not source.is_symlink():
                shutil.copy2(source, workspace / name)
    usage = result.usage or {}
    calls = previous_calls + int(started)
    output = {
        "status": {
            "cancelled": "interrupted",
            "deadline_exceeded": "timeout",
            "failed": "provider_error",
            "startup_error": "provider_error",
            "malformed_output": "provider_error",
        }.get(result.status, result.status),
        "final_response": response,
        "api_calls": calls,
        "agent_calls": calls,
        "session_id": result.session_id or previous.get("session_id", ""),
        "model_settings": list(result.model_settings),
        "usage_known": result.usage is not None,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "error": result.error,
        "artifacts": sorted(bridge.toolset.artifacts),
    }
    atomic_json_write(
        ledger_path,
        {
            "limit": None,
            "used": calls,
            "backend": backend,
            "research_policy": research_policy,
            "database_home": previous.get("database_home"),
            "session_id": result.session_id or previous.get("session_id", ""),
            "usage_total": (
                result.usage_total
                if result.usage_total is not None
                else previous.get("usage_total")
            ),
            "usage": usage,
            "unit": "agent_invocations",
            "model_settings": list(result.model_settings),
        },
    )
    emit("job-session-end", output)
    try:
        release_snapshot_dependencies(snapshot)
    except (OSError, ValueError) as exc:
        # Cache cleanup must not turn a durably recorded proof response into a lost result.
        emit("job-cache-cleanup-error", {"error": str(exc)})
    return output
