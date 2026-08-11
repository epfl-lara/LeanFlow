"""Keep unsafe managed-workflow artifacts out of prover file-tool context."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePath

from core.runtime_modes import env_flag_enabled

WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV = "LEANFLOW_DIAGNOSTIC_FILE_ACCESS"
MANAGED_PLAN_VIEW_MAX_CHARS = 8_000
MANAGED_MACHINE_SNAPSHOT_NAMES = frozenset({"blueprint.json", "summary.json"})

_PLAN_NOTES_HEADING_RE = re.compile(r"(?m)^(?:[ \t]*\d+\|)?## Notes[ \t]*$")


def diagnostic_workflow_file_access_enabled() -> bool:
    """Return whether direct workflow-artifact inspection is explicitly enabled."""
    return env_flag_enabled(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV)


def _normalized_parts(path: str) -> tuple[str, ...]:
    """Return portable, case-normalized path components for policy matching."""
    normalized = str(path or "").strip().replace("\\", "/")
    return tuple(
        component.lower()
        for component in PurePath(normalized).parts
        if component not in {"", ".", "/"}
    )


def is_workflow_state_path(path: str) -> bool:
    """Return whether a path directly targets LeanFlow's managed workflow state."""
    parts = _normalized_parts(path)
    return any(
        parts[index : index + 2] == (".leanflow", "workflow-state")
        for index in range(max(0, len(parts) - 1))
    )


def is_leanflow_internal_path(path: str) -> bool:
    """Return whether a path traverses LeanFlow's project-local metadata tree."""
    parts = _normalized_parts(path)
    for index in range(max(0, len(parts) - 2)):
        if parts[index : index + 3] == (".leanflow", "workspace", "repos"):
            return False
    return ".leanflow" in parts


def is_live_workflow_log_path(path: str) -> bool:
    """Return whether a path names a live or append-only workflow transcript."""
    parts = _normalized_parts(path)
    if not parts:
        return False
    if parts[-1] == "latest-run.log":
        return True
    return is_workflow_state_path(path) and parts[-1].endswith((".log", ".jsonl"))


def is_managed_plan_path(path: str) -> bool:
    """Return whether a path names the living managed ``plan.md`` render."""
    parts = _normalized_parts(path)
    if len(parts) >= 2 and parts[-2:] == ("workflow-state", "plan.md"):
        return True

    override = str(os.getenv("LEANFLOW_PLAN_STATE_DIR", "") or "").strip()
    if not override or not path:
        return False
    try:
        requested = Path(path).expanduser().resolve(strict=False)
        configured = (Path(override).expanduser() / "plan.md").resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return requested == configured


def is_managed_machine_snapshot_path(path: str) -> bool:
    """Return whether a path names a large model-unsafe plan-state snapshot."""
    parts = _normalized_parts(path)
    if (
        len(parts) >= 2
        and parts[-2] == "workflow-state"
        and parts[-1] in MANAGED_MACHINE_SNAPSHOT_NAMES
    ):
        return True

    override = str(os.getenv("LEANFLOW_PLAN_STATE_DIR", "") or "").strip()
    if not override or not path:
        return False
    try:
        requested = Path(path).expanduser().resolve(strict=False)
        root = Path(override).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return requested.parent == root and requested.name.lower() in MANAGED_MACHINE_SNAPSHOT_NAMES


def generated_plan_view(plan_text: str, *, max_chars: int = MANAGED_PLAN_VIEW_MAX_CHARS) -> str:
    """Return a bounded plan render with the historical Notes tail removed.

    Accept both raw Markdown and ``read_file``'s line-numbered representation.
    Keeping this transform below the CLI layer lets every model-facing file read
    use the same boundary as orchestrator prompt construction. Oversized views
    favor the current-state prefix and retain the recent decision/report tail;
    the middle marker authenticates and quantifies the omitted source.
    """
    text = str(plan_text or "")
    match = _PLAN_NOTES_HEADING_RE.search(text)
    generated = (text[: match.start()] if match else text).rstrip()
    ceiling = max(512, int(max_chars))
    if len(generated) <= ceiling:
        return generated
    source_sha256 = hashlib.sha256(generated.encode("utf-8")).hexdigest()
    marker = ""
    for _ in range(8):
        retained_chars = ceiling - len(marker)
        omitted_chars = max(0, len(generated) - retained_chars)
        updated = (
            "\n\n...[generated plan projection: "
            f"source_chars={len(generated)}; returned_chars={ceiling}; "
            f"omitted_source_chars={omitted_chars}; sha256={source_sha256}; "
            "historical_notes_excluded=true]...\n\n"
        )
        if updated == marker:
            break
        marker = updated
    available = ceiling - len(marker)
    # The render puts Goal, Current state, Strategy, and Frontier before the potentially large
    # Grounding ledger. Favor that authoritative current prefix while retaining the recent
    # Decision/Dead-end/Final-report tail.
    head_size = (available * 7) // 10
    tail_size = available - head_size
    return generated[:head_size] + marker + generated[-tail_size:]


def workflow_plan_pagination_error(path: str, offset: int) -> str | None:
    """Reject pagination that could start inside the preserved Notes tail."""
    if (
        diagnostic_workflow_file_access_enabled()
        or not is_managed_plan_path(path)
        or int(offset) <= 1
    ):
        return None
    return (
        "BLOCKED: managed plan.md exposes only its bounded generated view beginning at line 1. "
        "The `## Notes` tail is historical user-owned context and cannot be paged into by a "
        "prover or research agent. Use the injected queue assignment, dependency-graph digest, "
        "and current Lean source/kernel diagnostics as inventory truth. For an isolated operator "
        f"diagnostic only, set {WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV}=1."
    )


def workflow_machine_snapshot_read_error(path: str) -> str | None:
    """Reject raw model reads of large machine snapshots supplied as digests."""
    if diagnostic_workflow_file_access_enabled() or not is_managed_machine_snapshot_path(path):
        return None
    return (
        "BLOCKED: raw plan-state summary.json and blueprint.json are machine snapshots, not "
        "model context. They may contain megabytes of historical job records and stale stored "
        "declaration bodies. Use the injected dependency-graph digest, current queue assignment, "
        "completed-finding handoff, and Lean source/kernel diagnostics instead. For an isolated "
        f"operator diagnostic only, set {WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV}=1."
    )


def managed_plan_read_view(path: str, content: str) -> str:
    """Expose only the bounded generated plan sections to model tools.

    The Notes tail is user-owned historical context. Hiding both its content
    and write boundary prevents a model that cannot inspect prior notes from
    repeatedly appending semantically equivalent plan-route refreshes.
    """
    if diagnostic_workflow_file_access_enabled() or not is_managed_plan_path(path):
        return content
    return generated_plan_view(content, max_chars=MANAGED_PLAN_VIEW_MAX_CHARS)


def workflow_log_read_error(path: str) -> str | None:
    """Return a deterministic denial for direct live-transcript reads."""
    if diagnostic_workflow_file_access_enabled() or not is_live_workflow_log_path(path):
        return None
    return (
        "BLOCKED: managed workflow logs cannot be read by a prover or research agent. "
        "Reading this live transcript would feed the agent its own prior output and grow context "
        "recursively. Campaign plans, verified findings, and job results are supplied through "
        "structured workflow context. For an isolated operator diagnostic only, set "
        f"{WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV}=1."
    )


def workflow_state_search_error(path: str) -> str | None:
    """Return a deterministic denial for searches rooted in workflow state."""
    if diagnostic_workflow_file_access_enabled() or not (
        is_workflow_state_path(path) or is_live_workflow_log_path(path)
    ):
        return None
    return (
        "BLOCKED: search_files cannot search managed workflow-state or live log artifacts. "
        "Repository-wide searches exclude .leanflow by default; use the structured campaign "
        "context for plans and findings. For an isolated operator diagnostic only, set "
        f"{WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV}=1."
    )
