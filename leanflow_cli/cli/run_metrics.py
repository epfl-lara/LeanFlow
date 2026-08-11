"""Build immutable, run-scoped metrics and reproducibility evidence.

Metrics may be quoted in evaluations and papers, so this module never fills a
selected run from mutable project-wide ``latest`` files. Activity and usage are
read from the run's own stream/log; declarations, outcome, and provenance are
published only from an immutable snapshot sealed after native finalization.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from leanflow_cli.cli import run_provenance as _run_provenance
from leanflow_cli.cli import run_snapshot as _run_snapshot
from leanflow_cli.cli import run_stream as _run_stream

_FAILURE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("verification-rejected", ("verification-failed", "verification-rejected", "rejected-edit")),
    ("proof-attempt-rejected", ("proof-attempt-rejected", "attempt-rejected")),
    ("timeout", ("timeout", "timed-out", "deadline")),
    ("axiom-violation", ("axiom-profile", "axiom-violation", "forbidden-axiom")),
    ("rollback", ("rollback", "restored", "revert")),
    ("owner-conflict", ("owner-conflict", "live-status-owner")),
    ("provider-error", ("provider-error", "provider-exhaustion", "provider-retry", "api-error")),
    ("stalled", ("stalled", "stuck", "no-progress", "breakpoint")),
    ("blocked", ("blocked", "obstruction")),
)
_TERMINAL_EXIT_STATUSES = {
    0: "succeeded",
    2: "paused",
    3: "disproved",
    130: "interrupted",
}
_PROCESS_LAUNCH_COLLISIONS = _run_snapshot._PROCESS_LAUNCH_COLLISIONS


validate_run_id = _run_snapshot.validate_run_id
run_result_snapshot_path = _run_snapshot.run_result_snapshot_path
run_result_integrity_path = _run_snapshot.run_result_integrity_path
run_launch_snapshot_path = _run_snapshot.run_launch_snapshot_path
run_launch_collision_path = _run_snapshot.run_launch_collision_path
_launch_collision_key = _run_snapshot._launch_collision_key


_parse_count = _run_snapshot._parse_count
_parse_float = _run_stream._parse_float
_parse_exit_code = _run_snapshot._parse_exit_code


def terminal_status_for_exit_code(exit_code: int | None) -> str:
    """Return the stable terminal status associated with an exact exit code."""
    if exit_code is None:
        return "exited"
    return _TERMINAL_EXIT_STATUSES.get(exit_code, "failed")


def _classify_failure(event_type: str) -> str | None:
    lowered = event_type.lower()
    for bucket, needles in _FAILURE_PATTERNS:
        if any(needle in lowered for needle in needles):
            return bucket
    return None


def _git_bytes(project_root: Path, *args: str) -> bytes | None:
    """Run one read-only git command and preserve its exact byte output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


_sha256_bytes = _run_snapshot._sha256_bytes


def _runtime_source_identity() -> dict[str, Any]:
    """Return runtime identity through the stable patch-sensitive surface."""
    return _run_provenance._runtime_source_identity(module_file=__file__)


def _python_runtime_identity(project_root: Path | None = None) -> dict[str, Any]:
    """Return Python identity through the stable metadata patch surface."""
    return _run_provenance._python_runtime_identity(
        distribution_provider=importlib_metadata.distributions,
        project_root=project_root,
    )


def collect_provenance(project_root: Path) -> dict[str, Any]:
    """Return provenance while preserving this module's Git discovery seam."""
    return _run_provenance.collect_provenance(
        project_root,
        git_bytes=_git_bytes,
        runtime_module_file=__file__,
        distribution_provider=importlib_metadata.distributions,
    )


def collect_launch_environment(
    environment: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Return the canonical redacted launch environment."""
    return _run_provenance.collect_launch_environment(environment, project_root=project_root)


def _launch_environment_is_valid(environment: Mapping[str, Any] | None) -> bool:
    """Return whether one launch environment has valid canonical evidence."""
    return _run_provenance._launch_environment_is_valid(environment)


def _provenance_is_valid(provenance: Mapping[str, Any] | None) -> bool:
    """Return whether one provenance payload has valid exact evidence."""
    return _run_provenance._provenance_is_valid(provenance)


_read_json = _run_snapshot._read_json
_write_json_exclusive = _run_snapshot._write_json_exclusive
_snapshot_sha256 = _run_snapshot._snapshot_sha256
_harden_snapshot_read_only = _run_snapshot._harden_snapshot_read_only
read_integrity_checked_run_result = _run_snapshot.read_integrity_checked_run_result
_declaration_rows = _run_snapshot._declaration_rows


def capture_run_launch_snapshot(
    state_root: Path,
    *,
    run_id: str,
    project_root: Path,
    context: Mapping[str, Any] | None = None,
) -> bool:
    """Seal launch evidence through the stable provenance facade."""
    return _run_snapshot.capture_run_launch_snapshot(
        state_root,
        run_id=run_id,
        project_root=project_root,
        context=context,
        launch_environment_collector=collect_launch_environment,
        provenance_collector=collect_provenance,
    )


_RunStream = _run_stream._RunStream
_iter_jsonl = _run_stream._iter_jsonl
_journal_rejections = _run_stream._journal_rejections
_events_sha256 = _run_stream._events_sha256
_single_run_lifecycle = _run_stream._single_run_lifecycle
_read_hot_stream = _run_stream._read_hot_stream
_archive_audit_payload = _run_stream._archive_audit_payload
_read_retained_stream = _run_stream._read_retained_stream
_select_run_stream = _run_stream._select_run_stream
_run_log_path = _run_stream._run_log_path
_usage_from_log = _run_stream._usage_from_log
_api_request_evidence = _run_stream._api_request_evidence
_aggregate_usage = _run_stream._aggregate_usage


def finalize_run_snapshot(
    state_root: Path,
    *,
    run_id: str,
    project_root: Path,
    outcome: Mapping[str, Any],
) -> bool:
    """Seal final evidence through the stable provenance facade."""
    return _run_snapshot.finalize_run_snapshot(
        state_root,
        run_id=run_id,
        project_root=project_root,
        outcome=outcome,
        provenance_collector=collect_provenance,
    )


def _verified_snapshot(state_root: Path, stream: _RunStream) -> tuple[dict[str, Any], str]:
    if not stream.run_id:
        return {}, "run-not-found"
    snapshot, integrity_status = read_integrity_checked_run_result(state_root, stream.run_id)
    if not snapshot:
        return {}, integrity_status
    identity = snapshot.get("stream")
    if not isinstance(identity, Mapping):
        return {}, "final-snapshot-stream-identity-missing"
    if _parse_count(identity.get("event_count")) != len(stream.events) or str(
        identity.get("events_sha256", "") or ""
    ) != _events_sha256(stream.events):
        return {}, "final-snapshot-stream-mismatch"
    return snapshot, "verified"


_event_run_metadata = _run_stream._event_run_metadata


def collect_run_metrics(
    state_root: Path,
    *,
    run_id: str = "",
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Aggregate one exact run without consulting mutable project-wide state."""
    stream = _select_run_stream(state_root, run_id)
    events = list(stream.events)
    by_type: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    timestamps: list[str] = []
    for event in events:
        event_type = str(event.get("type", "") or "unknown")
        by_type[event_type] += 1
        bucket = _classify_failure(event_type)
        if bucket:
            failures[bucket] += 1
        timestamp = str(event.get("timestamp", "") or "")
        if timestamp:
            timestamps.append(timestamp)

    duration_s: float | None = None
    if timestamps:
        try:
            duration_s = (
                datetime.fromisoformat(timestamps[-1]) - datetime.fromisoformat(timestamps[0])
            ).total_seconds()
        except ValueError:
            pass
    terminal_event = events[-1] if events else {}
    terminal = str(terminal_event.get("type", "") or "") == "runner-exit"
    lifecycle_valid = _single_run_lifecycle(events)
    stream_complete = bool(
        stream.run_id and stream.integrity_complete and terminal and lifecycle_valid
    )
    snapshot, snapshot_status = _verified_snapshot(state_root, stream)
    snapshot_verified = bool(snapshot)
    metadata = _event_run_metadata(events)
    usage = _aggregate_usage(
        events,
        _run_log_path(state_root, stream.run_id) if stream.run_id else state_root / "missing.log",
    )

    launch = snapshot.get("launch") if snapshot_verified else None
    launch = dict(launch) if isinstance(launch, Mapping) else {}
    launch_identity_valid = bool(
        launch
        and str(launch.get("run_id", "") or "") == stream.run_id
        and _parse_count(launch.get("version")) == 1
    )
    launch_environment = launch.get("environment")
    launch_environment = (
        dict(launch_environment) if isinstance(launch_environment, Mapping) else None
    )
    launch_environment_valid = _launch_environment_is_valid(launch_environment)
    provenance = launch.get("provenance") if project_root is not None else None
    provenance = dict(provenance) if isinstance(provenance, Mapping) else None
    provenance_valid = project_root is None or _provenance_is_valid(provenance)
    final_provenance = (
        snapshot.get("final_provenance") if snapshot_verified and project_root is not None else None
    )
    final_provenance = dict(final_provenance) if isinstance(final_provenance, Mapping) else None
    final_provenance_valid = project_root is None or _provenance_is_valid(final_provenance)
    raw_final_outcome = snapshot.get("outcome") if snapshot_verified else None
    snapshot_outcome = dict(raw_final_outcome) if isinstance(raw_final_outcome, Mapping) else {}
    terminal_details = terminal_event.get("details")
    terminal_details = terminal_details if isinstance(terminal_details, Mapping) else {}
    terminal_exit_code = _parse_exit_code(terminal_details.get("exit_code")) if terminal else None
    outcome_exit_code = _parse_exit_code(snapshot_outcome.get("exit_code"))
    terminal_evidence_valid = bool(
        terminal_exit_code is not None
        and outcome_exit_code is not None
        and terminal_exit_code == outcome_exit_code
    )
    snapshot_evidence = snapshot.get("evidence") if snapshot_verified else None
    launch_collision_absent = bool(
        isinstance(snapshot_evidence, Mapping)
        and snapshot_evidence.get("launch_snapshot_collision") is False
    )
    provenance_comparable = bool(
        project_root is not None
        and provenance_valid
        and final_provenance_valid
        and provenance is not None
        and final_provenance is not None
    )

    def provenance_field_changed(field: str) -> bool | None:
        if not provenance_comparable or provenance is None or final_provenance is None:
            return None
        return provenance.get(field) != final_provenance.get(field)

    runtime_source_changed = provenance_field_changed("runtime_source_sha256")
    python_runtime_changed = provenance_field_changed("python_runtime_sha256")
    selected_skills_changed = provenance_field_changed("selected_skills_sha256")
    behavior_config_changed = provenance_field_changed("behavior_config_sha256")
    lean_toolchain_changed = provenance_field_changed("lean_toolchain")
    dependency_manifest_changed = provenance_field_changed("dependency_manifest_sha256")
    build_configuration_changed = provenance_field_changed("build_configuration_sha256")
    project_configuration_changed = (
        bool(
            provenance_field_changed("project_manifest_sha256")
            or provenance_field_changed("workflow_guidance_sha256")
        )
        if provenance_comparable
        else None
    )
    non_source_inputs_unchanged = bool(
        project_root is None
        or (
            provenance_comparable
            and runtime_source_changed is False
            and python_runtime_changed is False
            and selected_skills_changed is False
            and behavior_config_changed is False
            and project_configuration_changed is False
            and lean_toolchain_changed is False
            and dependency_manifest_changed is False
            and build_configuration_changed is False
        )
    )
    usage_complete = usage.get("complete") is True
    exact = bool(
        stream_complete
        and snapshot_verified
        and launch_identity_valid
        and launch_environment_valid
        and terminal_evidence_valid
        and launch_collision_absent
        and provenance_valid
        and final_provenance_valid
        and non_source_inputs_unchanged
        and usage_complete
        and usage.get("descendant_usage_complete") is True
        and (_parse_count(usage.get("command_expert_attempts")) or 0) == 0
    )
    declarations = list(snapshot.get("declarations") or []) if exact else []
    raw_counts = snapshot.get("declaration_status_counts") if exact else None
    status_counts = dict(raw_counts) if isinstance(raw_counts, Mapping) else {}
    raw_outcome = snapshot_outcome if exact else None
    final_outcome = dict(raw_outcome) if isinstance(raw_outcome, Mapping) else {}
    missing: list[str] = []
    if not stream.run_id:
        missing.append("run-stream")
    elif not stream.integrity_complete:
        missing.append("verified-complete-stream")
    if not terminal:
        missing.append("runner-exit")
    if stream.run_id and not lifecycle_valid:
        missing.append("single-run-lifecycle")
    if not snapshot:
        missing.append(snapshot_status)
    if snapshot_verified and not launch_identity_valid:
        missing.append("launch-snapshot-identity")
    if snapshot_verified and not provenance_valid:
        missing.append("launch-provenance")
    if snapshot_verified and not final_provenance_valid:
        missing.append("final-provenance")
    if runtime_source_changed:
        missing.append("runtime-source-drift")
    if python_runtime_changed:
        missing.append("python-runtime-drift")
    if selected_skills_changed:
        missing.append("selected-skills-drift")
    if behavior_config_changed:
        missing.append("behavior-config-drift")
    if project_configuration_changed:
        missing.append("project-configuration-drift")
    if lean_toolchain_changed:
        missing.append("lean-toolchain-drift")
    if dependency_manifest_changed:
        missing.append("dependency-manifest-drift")
    if build_configuration_changed:
        missing.append("build-configuration-drift")
    if not usage_complete:
        missing.append("complete-usage-evidence")
    if usage.get("descendant_usage_complete") is not True:
        missing.append("descendant-run-usage-unmetered")
    if (_parse_count(usage.get("command_expert_attempts")) or 0) > 0:
        missing.append("external-command-runtime-unpinned")
    if snapshot_verified and not launch_environment_valid:
        missing.append("launch-environment")
    if snapshot_verified and not launch_collision_absent:
        missing.append("launch-snapshot-collision")
    if terminal and terminal_exit_code is None:
        missing.append("runner-exit-code")
    if snapshot_verified and outcome_exit_code is None:
        missing.append("final-outcome-exit-code")
    if (
        terminal_exit_code is not None
        and outcome_exit_code is not None
        and terminal_exit_code != outcome_exit_code
    ):
        missing.append("terminal-exit-code-mismatch")

    journal_raw = snapshot.get("journal_rejections") if exact else None
    journal = dict(journal_raw) if isinstance(journal_raw, Mapping) else {}
    journal_scope = snapshot.get("journal_rejections_scope") if exact else None
    proof_solved = final_outcome.get("proof_solved") if "proof_solved" in final_outcome else None
    payload: dict[str, Any] = {
        "version": 2,
        "scope": {
            "requested_run_id": validate_run_id(run_id, allow_empty=True),
            "resolved_run_id": stream.run_id,
            "run_found": bool(stream.run_id and events),
            "stream_source": stream.source,
            "stream_integrity_complete": stream.integrity_complete,
            "terminal_event_seen": terminal,
            "single_run_lifecycle": lifecycle_valid,
            "final_snapshot": snapshot_status,
            "exact": exact,
            "missing": missing,
            "archive_audit": stream.archive_audit,
        },
        "run": {
            "run_id": stream.run_id,
            **metadata,
            "started_at": timestamps[0] if timestamps else "",
            "updated_at": timestamps[-1] if timestamps else "",
            "duration_s": duration_s,
        },
        "launch": (
            {
                "captured_at": launch.get("captured_at"),
                "context": dict(launch.get("context") or {}),
                "environment": launch_environment,
            }
            if exact
            else None
        ),
        "events": {
            "total": sum(by_type.values()),
            "complete": stream_complete,
            "by_type": dict(by_type.most_common()),
        },
        "activity": {
            "tool_calls": by_type.get("tool-call", 0),
            "tool_results": by_type.get("tool-result", 0),
            "api_requests": usage["recorded_api_requests"],
            "assistant_responses": by_type.get("assistant-response", 0),
            "dispatch_jobs": by_type.get("dispatch-job", 0),
        },
        "failures": dict(failures.most_common()),
        "journal_rejections": journal,
        "journal_rejections_scope": journal_scope,
        "usage": usage,
        "declarations": declarations,
        "declaration_status_counts": status_counts,
        "outcome": {
            "phase": final_outcome.get("phase"),
            "exit_code": outcome_exit_code if exact else None,
            "terminal_status": (
                terminal_status_for_exit_code(outcome_exit_code) if exact else None
            ),
            "reason": final_outcome.get("reason"),
            "proof_solved": proof_solved if isinstance(proof_solved, bool) else None,
            "sorry_count": final_outcome.get("sorry_count"),
            "project_sorry_count": final_outcome.get("project_sorry_count"),
            "model": final_outcome.get("model"),
            "provider": final_outcome.get("provider"),
            "declarations_total": len(declarations) if exact else None,
            "declarations_proved": (
                sum(1 for row in declarations if isinstance(row, Mapping) and row.get("proved"))
                if exact
                else None
            ),
        },
    }
    if project_root is not None:
        payload["provenance"] = provenance
        payload["provenance_final"] = final_provenance
        payload["source_changed_during_run"] = (
            provenance.get("source_identity_sha256")
            != final_provenance.get("source_identity_sha256")
            if provenance_valid
            and final_provenance_valid
            and provenance is not None
            and final_provenance is not None
            else None
        )
        payload["runtime_source_changed"] = runtime_source_changed
        payload["python_runtime_changed"] = python_runtime_changed
        payload["selected_skills_changed"] = selected_skills_changed
        payload["behavior_config_changed"] = behavior_config_changed
        payload["project_configuration_changed"] = project_configuration_changed
        payload["lean_toolchain_changed"] = lean_toolchain_changed
        payload["dependency_manifest_changed"] = dependency_manifest_changed
        payload["build_configuration_changed"] = build_configuration_changed
    return payload
