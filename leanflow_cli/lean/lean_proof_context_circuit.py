"""Persist campaign-scoped proof-context timeout circuit breakers.

The managed proof-context backend can spend its full request budget scanning a
large imported environment before timing out.  A process-local disable prevents
repeat calls only until the native runner restarts.  This module records the
narrow timeout signature in the durable campaign summary so resumed processes
can use the exact local declaration fallback immediately.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

PROOF_CONTEXT_CIRCUIT_KEY = "proof_context_timeout_circuits"
PROOF_CONTEXT_CIRCUIT_CAP = 8
PROOF_CONTEXT_SLOW_FAILURE_SECONDS = 90.0

_TIMEOUT_RE = re.compile(
    r"(?:\bTimeout(?:Error|Expired)\b|\btimed\s+out\b|\bdeadline\s+exceeded\b)",
    flags=re.IGNORECASE,
)


def _now_iso() -> str:
    """Return a stable UTC timestamp for persisted circuit records."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _summary_path() -> Path:
    """Return the active project's durable campaign summary path."""
    return workflow_state_root() / "summary.json"


def _project_root_text(cwd: str | Path | None) -> str:
    """Return a canonical directory key for a proof-context backend call."""
    if cwd is None:
        return ""
    candidate = Path(cwd).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    return str(candidate)


def is_timeout_failure(message: str, *, elapsed_s: float = 0.0) -> bool:
    """Return whether a failed backend call carries timeout-strength evidence.

    lean-proof-auto currently catches its internal LeanInteract 120-second
    timeout and sometimes returns only ``status=error``.  A failed call that
    consumed at least 90 seconds is therefore treated as equivalent evidence;
    callers never apply this elapsed-time rule to successful responses.
    """
    return bool(_TIMEOUT_RE.search(str(message or ""))) or float(elapsed_s or 0.0) >= (
        PROOF_CONTEXT_SLOW_FAILURE_SECONDS
    )


def _positive_count(value: Any) -> int:
    """Return a persisted observation count as a positive integer."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _nonnegative_float(value: Any) -> float:
    """Return a persisted duration as a non-negative float."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _normalized_entries(raw: Any) -> list[dict[str, Any]]:
    """Return valid deduplicated proof-context timeout circuit records."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    entries: list[dict[str, Any]] = []
    by_signature: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind", "") or "") != "proof_context_backend_timeout":
            continue
        tool_name = str(item.get("tool_name", "") or "").strip()
        project_root = str(item.get("project_root", "") or "").strip()
        if not tool_name or not project_root:
            continue
        signature = f"proof-context-timeout:{project_root}:{tool_name}"
        entry = {
            "signature": signature,
            "kind": "proof_context_backend_timeout",
            "tool_name": tool_name,
            "project_root": project_root,
            "file_path": str(item.get("file_path", "") or ""),
            "theorem_id": str(item.get("theorem_id", "") or ""),
            "failure": str(item.get("failure", "") or "")[:240],
            "elapsed_s": _nonnegative_float(item.get("elapsed_s", 0.0)),
            "count": _positive_count(item.get("count", 1)),
            "first_seen_at": str(item.get("first_seen_at", "") or ""),
            "last_seen_at": str(item.get("last_seen_at", "") or ""),
        }
        previous_index = by_signature.get(signature)
        if previous_index is None:
            by_signature[signature] = len(entries)
            entries.append(entry)
        else:
            entries[previous_index] = entry
    return entries[-PROOF_CONTEXT_CIRCUIT_CAP:]


def timed_out_tools(*, cwd: str | Path | None) -> set[str]:
    """Return proof-context tools disabled by the active campaign's timeout memory."""
    project_root = _project_root_text(cwd)
    if not project_root:
        return set()
    summary = read_json_file(_summary_path())
    campaign = dict(summary.get("campaign") or {})
    if not str(campaign.get("campaign_id", "") or "").strip():
        return set()
    return {
        str(entry["tool_name"])
        for entry in _normalized_entries(campaign.get(PROOF_CONTEXT_CIRCUIT_KEY))
        if str(entry.get("project_root", "") or "") == project_root
    }


def declaration_scan_timed_out(*, cwd: str | Path | None) -> bool:
    """Return whether proof-auto's shared declaration scanner timed out here."""
    return bool(timed_out_tools(cwd=cwd))


def record_timeout(
    tool_name: str,
    failure: str,
    *,
    cwd: str | Path | None,
    file_path: str = "",
    theorem_id: str = "",
    elapsed_s: float = 0.0,
) -> bool:
    """Persist one verified-local-fallback backend timeout for the active campaign.

    Generic failures are deliberately ignored.  Callers should invoke this only
    after confirming that local declaration extraction can serve the request, so
    opening the circuit cannot remove the only available proof-context path.
    """
    normalized_tool = str(tool_name or "").strip()
    normalized_failure = str(failure or "").strip()
    project_root = _project_root_text(cwd)
    normalized_elapsed_s = _nonnegative_float(elapsed_s)
    if (
        not normalized_tool
        or not project_root
        or not is_timeout_failure(normalized_failure, elapsed_s=normalized_elapsed_s)
    ):
        return False
    observed_at = _now_iso()
    signature = f"proof-context-timeout:{project_root}:{normalized_tool}"

    def mutate(summary: dict[str, Any]) -> bool:
        campaign = dict(summary.get("campaign") or {})
        if not str(campaign.get("campaign_id", "") or "").strip():
            return False
        entries = _normalized_entries(campaign.get(PROOF_CONTEXT_CIRCUIT_KEY))
        by_signature = {str(entry["signature"]): dict(entry) for entry in entries}
        previous = dict(by_signature.get(signature) or {})
        by_signature[signature] = {
            "signature": signature,
            "kind": "proof_context_backend_timeout",
            "tool_name": normalized_tool,
            "project_root": project_root,
            "file_path": str(file_path or ""),
            "theorem_id": str(theorem_id or ""),
            "failure": normalized_failure[:240],
            "elapsed_s": round(normalized_elapsed_s, 3),
            "count": int(previous.get("count", 0) or 0) + 1,
            "first_seen_at": str(previous.get("first_seen_at", "") or observed_at),
            "last_seen_at": observed_at,
        }
        campaign[PROOF_CONTEXT_CIRCUIT_KEY] = list(by_signature.values())[
            -PROOF_CONTEXT_CIRCUIT_CAP:
        ]
        campaign["updated_at"] = observed_at
        summary["campaign"] = campaign
        return True

    return bool(update_json_file(_summary_path(), mutate))
