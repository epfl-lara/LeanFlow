"""Persist advisor failure budgets across managed process lifetimes."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

ADVISOR_TOOL_NAMES = frozenset({"lean_reasoning_help", "lean_decompose_helpers"})
FAILURE_THRESHOLD = 2
STATE_VERSION = 3
LEGACY_STATE_VERSIONS = frozenset({1, 2})
PENDING_STATE_KEY = "_pending_advisor_source_revisions"


@dataclass(frozen=True)
class AdvisorFailureSnapshot:
    """Describe the durable residual-target and campaign-timeout circuit."""

    target_symbol: str = ""
    active_file: str = ""
    source_revision_sha256: str = ""
    target_revision_sha256: str = ""
    evidence_revision_sha256: str = ""
    campaign_id: str = ""
    consecutive_failures: int = 0
    last_status: str = ""


@dataclass(frozen=True)
class AdvisorCallIdentity:
    """Identify the file and residual declaration seen by one advisor call."""

    source_revision_sha256: str = ""
    target_revision_sha256: str = ""
    evidence_revision_sha256: str = ""


def _state_path() -> Path:
    """Return the workflow-local advisor circuit path."""
    return workflow_state_root() / "advisor-failure-circuit.json"


def _canonical_file(value: Any) -> str:
    """Return a stable active-file identity across relative and absolute callers."""
    text = str(value or "").strip()
    if not text:
        return ""
    project_root = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd())
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        expanded = os.path.join(project_root, expanded)
    return os.path.realpath(expanded)


def _snapshot(payload: Mapping[str, Any] | None) -> AdvisorFailureSnapshot:
    """Normalize one persisted circuit payload."""
    raw = dict(payload or {})
    if int(raw.get("version", 0) or 0) not in {
        *LEGACY_STATE_VERSIONS,
        STATE_VERSION,
    }:
        return AdvisorFailureSnapshot()
    return AdvisorFailureSnapshot(
        target_symbol=str(raw.get("target_symbol", "") or "").strip(),
        active_file=_canonical_file(raw.get("active_file", "")),
        source_revision_sha256=str(raw.get("source_revision_sha256", "") or "").strip(),
        target_revision_sha256=str(raw.get("target_revision_sha256", "") or "").strip(),
        evidence_revision_sha256=str(raw.get("evidence_revision_sha256", "") or "").strip(),
        campaign_id=str(raw.get("campaign_id", "") or "").strip(),
        consecutive_failures=max(0, int(raw.get("consecutive_failures", 0) or 0)),
        last_status=str(raw.get("last_status", "") or "").strip(),
    )


def load_snapshot() -> AdvisorFailureSnapshot:
    """Return the current durable circuit, failing open on unreadable state."""
    try:
        return _snapshot(read_json_file(_state_path()))
    except (OSError, TypeError, ValueError):
        return AdvisorFailureSnapshot()


def _matches(
    snapshot: AdvisorFailureSnapshot,
    *,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
    target_revision_sha256: str,
    evidence_revision_sha256: str,
    campaign_id: str,
) -> bool:
    """Return whether a snapshot owns the current campaign residual target."""
    incoming_campaign = str(campaign_id or "").strip()
    snapshot_target = str(snapshot.target_revision_sha256 or "").strip()
    incoming_target = str(target_revision_sha256 or "").strip()
    incoming_evidence = str(evidence_revision_sha256 or "").strip()
    revision_matches = (
        snapshot_target == incoming_target
        if snapshot_target and incoming_target
        else snapshot.source_revision_sha256 == str(source_revision_sha256 or "").strip()
    )
    campaign_timeout_quarantine = bool(
        snapshot.last_status == "timeout"
        and snapshot.consecutive_failures >= FAILURE_THRESHOLD
        and snapshot.campaign_id
        and incoming_campaign
        and snapshot.campaign_id == incoming_campaign
    )
    return bool(
        snapshot.target_symbol == str(target_symbol or "").strip()
        and snapshot.active_file == _canonical_file(active_file)
        and snapshot.evidence_revision_sha256 == incoming_evidence
        and (revision_matches or campaign_timeout_quarantine)
        and (
            not snapshot.campaign_id
            or not incoming_campaign
            or snapshot.campaign_id == incoming_campaign
        )
    )


def preflight_blocked(
    *,
    function_name: str,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
    target_revision_sha256: str = "",
    evidence_revision_sha256: str = "",
    campaign_id: str = "",
) -> bool:
    """Return whether the residual budget or campaign timeout quarantine is exhausted."""
    if str(function_name or "").strip() not in ADVISOR_TOOL_NAMES:
        return False
    snapshot = load_snapshot()
    return (
        _matches(
            snapshot,
            target_symbol=target_symbol,
            active_file=active_file,
            source_revision_sha256=source_revision_sha256,
            target_revision_sha256=target_revision_sha256,
            evidence_revision_sha256=evidence_revision_sha256,
            campaign_id=campaign_id,
        )
        and snapshot.consecutive_failures >= FAILURE_THRESHOLD
    )


def remember_call_source(
    state: dict[str, Any],
    *,
    function_name: str,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
    target_revision_sha256: str = "",
    evidence_revision_sha256: str = "",
) -> None:
    """Remember the exact source and target one in-flight advisor request received."""
    tool = str(function_name or "").strip()
    if tool not in ADVISOR_TOOL_NAMES:
        return
    pending = dict(state.get(PENDING_STATE_KEY) or {})
    pending[tool] = {
        "target_symbol": str(target_symbol or "").strip(),
        "active_file": _canonical_file(active_file),
        "source_revision_sha256": str(source_revision_sha256 or "").strip(),
        "target_revision_sha256": str(target_revision_sha256 or "").strip(),
        "evidence_revision_sha256": str(evidence_revision_sha256 or "").strip(),
    }
    state[PENDING_STATE_KEY] = pending


def consume_call_identity(
    state: dict[str, Any],
    *,
    function_name: str,
    target_symbol: str,
    active_file: str,
    fallback_source_revision_sha256: str,
    fallback_target_revision_sha256: str = "",
    fallback_evidence_revision_sha256: str = "",
) -> AdvisorCallIdentity:
    """Return and clear the source identity owned by one completed advisor call."""
    tool = str(function_name or "").strip()
    pending = dict(state.get(PENDING_STATE_KEY) or {})
    record = dict(pending.pop(tool, {}) or {})
    if pending:
        state[PENDING_STATE_KEY] = pending
    else:
        state.pop(PENDING_STATE_KEY, None)
    if str(record.get("target_symbol", "") or "").strip() == str(
        target_symbol or ""
    ).strip() and _canonical_file(record.get("active_file", "")) == _canonical_file(active_file):
        recorded_revision = str(record.get("source_revision_sha256", "") or "").strip()
        recorded_target = str(record.get("target_revision_sha256", "") or "").strip()
        recorded_evidence = str(record.get("evidence_revision_sha256", "") or "").strip()
        if recorded_revision or recorded_target or recorded_evidence:
            return AdvisorCallIdentity(
                source_revision_sha256=(
                    recorded_revision or str(fallback_source_revision_sha256 or "").strip()
                ),
                target_revision_sha256=(
                    recorded_target or str(fallback_target_revision_sha256 or "").strip()
                ),
                evidence_revision_sha256=(
                    recorded_evidence or str(fallback_evidence_revision_sha256 or "").strip()
                ),
            )
    return AdvisorCallIdentity(
        source_revision_sha256=str(fallback_source_revision_sha256 or "").strip(),
        target_revision_sha256=str(fallback_target_revision_sha256 or "").strip(),
        evidence_revision_sha256=str(fallback_evidence_revision_sha256 or "").strip(),
    )


def consume_call_source(
    state: dict[str, Any],
    *,
    function_name: str,
    target_symbol: str,
    active_file: str,
    fallback_source_revision_sha256: str,
) -> str:
    """Return the preflight file revision for compatibility callers."""
    return consume_call_identity(
        state,
        function_name=function_name,
        target_symbol=target_symbol,
        active_file=active_file,
        fallback_source_revision_sha256=fallback_source_revision_sha256,
    ).source_revision_sha256


def _result_payload(result_text: str) -> dict[str, Any]:
    """Return one advisor result object or an empty payload."""
    try:
        payload = json.loads(str(result_text or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _failed_provider_result(payload: Mapping[str, Any]) -> bool:
    """Return whether a provider-backed advisor request supplied no answer."""
    if payload.get("provider_called") is False:
        return False
    if payload.get("success") is True:
        return False
    status = str(payload.get("status", "") or "").strip().lower()
    return payload.get("success") is False or status in {
        "error",
        "invalid_json",
        "no_answer",
        "timeout",
        "unavailable",
    }


def _failure_weight(status: str) -> int:
    """Charge a full retry budget for a provider call that reaches its timeout."""
    return FAILURE_THRESHOLD if str(status or "").strip().lower() == "timeout" else 1


def observe_result(
    *,
    function_name: str,
    result_text: str,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
    target_revision_sha256: str = "",
    evidence_revision_sha256: str = "",
    campaign_id: str = "",
) -> AdvisorFailureSnapshot:
    """Record one advisor result and return the resulting durable circuit."""
    if str(function_name or "").strip() not in ADVISOR_TOOL_NAMES:
        return load_snapshot()
    payload = _result_payload(result_text)
    if not payload:
        return load_snapshot()
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    revision = str(source_revision_sha256 or "").strip()
    target_revision = str(target_revision_sha256 or "").strip()
    evidence_revision = str(evidence_revision_sha256 or "").strip()
    campaign = str(campaign_id or "").strip()
    if not target or not active or not revision:
        return load_snapshot()

    if not _failed_provider_result(payload):
        if payload.get("success") is not True:
            return load_snapshot()

        def clear_if_matching(current: dict[str, Any]) -> AdvisorFailureSnapshot:
            snapshot = _snapshot(current)
            if _matches(
                snapshot,
                target_symbol=target,
                active_file=active,
                source_revision_sha256=revision,
                target_revision_sha256=target_revision,
                evidence_revision_sha256=evidence_revision,
                campaign_id=campaign,
            ):
                current.clear()
            return AdvisorFailureSnapshot()

        update_json_file(_state_path(), clear_if_matching)
        return load_snapshot()

    status = str(payload.get("status", "") or "").strip().lower() or "error"
    failure_weight = _failure_weight(status)
    recorded: AdvisorFailureSnapshot = AdvisorFailureSnapshot()

    def record(current: dict[str, Any]) -> AdvisorFailureSnapshot:
        nonlocal recorded
        prior = _snapshot(current)
        consecutive = (
            prior.consecutive_failures + failure_weight
            if _matches(
                prior,
                target_symbol=target,
                active_file=active,
                source_revision_sha256=revision,
                target_revision_sha256=target_revision,
                evidence_revision_sha256=evidence_revision,
                campaign_id=campaign,
            )
            else failure_weight
        )
        current.clear()
        current.update(
            {
                "version": STATE_VERSION,
                "target_symbol": target,
                "active_file": active,
                "source_revision_sha256": revision,
                "target_revision_sha256": target_revision,
                "evidence_revision_sha256": evidence_revision,
                "campaign_id": campaign,
                "consecutive_failures": consecutive,
                "last_status": status,
            }
        )
        recorded = _snapshot(current)
        return recorded

    update_json_file(_state_path(), record)
    return recorded
