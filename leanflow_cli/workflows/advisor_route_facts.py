"""Persist bounded target-scoped route exclusions from direct Lean advisors.

Advisor output is never proof or disproof evidence. This module retains only
negative route facts that would otherwise disappear with the model context,
ties them to the current declaration signature, and deliberately drops action
recommendations and terminal prose.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.plan_state import plan_state_enabled, plan_state_paths
from leanflow_cli.workflows.queue_edit_guard import _queue_edit_assigned_statement_signature
from leanflow_cli.workflows.workflow_json_io import update_json_file

SUMMARY_KEY = "advisor_route_facts"
VERIFICATION_LABEL = "advisor_unverified_route_evidence"
PER_TARGET_FACT_CAP = 6
GLOBAL_FACT_CAP = 32
FACT_TEXT_CAP = 3_200
PARAGRAPH_CAP = 900

_NEGATIVE_ROUTE_RE = re.compile(
    r"(?:"
    r"\b(?:cannot|can't|does\s+not|doesn't|fails?|failed|impossible|circular|"
    r"obstruction|counterexample|refut(?:e|es|ed|ation)|excluded?|incompatible)\b"
    r"|\bno\s+(?:admissible|required|suitable|such|valid|possible)\b"
    r")",
    flags=re.IGNORECASE,
)
_DROP_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:Continuation\s+route|LeanFlow\s+persistence\s+contract)\s*:",
    flags=re.IGNORECASE,
)


def _now_iso() -> str:
    """Return a stable UTC timestamp for one persisted observation."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _same_file(left: str, right: str) -> bool:
    """Return whether two assignment paths identify the same source file."""
    if not left or not right:
        return left == right
    project_root = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd())

    def canonical(value: str) -> str:
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            expanded = os.path.join(project_root, expanded)
        return os.path.realpath(expanded)

    return canonical(left) == canonical(right)


def declaration_signature_sha256(target_symbol: str, active_file: str) -> str:
    """Hash the current declaration signature while ignoring its proof body."""
    target = str(target_symbol or "").strip()
    path = str(active_file or "").strip()
    if not target or not path:
        return ""
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return ""
    signature = _queue_edit_assigned_statement_signature(content, target)
    if not signature:
        return ""
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _negative_route_fact(advice: str) -> str:
    """Extract bounded negative route evidence without advisor action prose."""
    text = str(advice or "").strip()
    if not text:
        return ""
    drop_match = _DROP_SECTION_RE.search(text)
    if drop_match:
        text = text[: drop_match.start()].rstrip()
    selected: list[str] = []
    for paragraph in re.split(r"\n{2,}|(?<=[.!?])\s+(?=[A-Z0-9`])", text):
        normalized = " ".join(str(paragraph or "").split()).strip()
        if not normalized or not _NEGATIVE_ROUTE_RE.search(normalized):
            continue
        selected.append(normalized[:PARAGRAPH_CAP])
        if len("\n\n".join(selected)) >= FACT_TEXT_CAP:
            break
    return "\n\n".join(selected)[:FACT_TEXT_CAP].strip()


def _campaign_id(summary: Mapping[str, Any]) -> str:
    """Return the active campaign identity recorded in a shared summary."""
    raw = summary.get("campaign")
    if isinstance(raw, Mapping):
        return str(raw.get("campaign_id", "") or "").strip()
    return ""


def _record_matches_target(
    record: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether a record belongs to one exact declaration assignment."""
    return bool(
        str(record.get("target_symbol", "") or "") == target_symbol
        and _same_file(str(record.get("active_file", "") or ""), active_file)
    )


def record_managed_advisor_result(
    *,
    function_name: str,
    result_text: str,
    target_symbol: str,
    active_file: str,
    campaign_id: str = "",
) -> dict[str, Any] | None:
    """Persist one exact-target advisor route exclusion, failing closed.

    Only successful, complete ``lean_reasoning_help`` payloads whose embedded
    assignment exactly matches the deterministic queue assignment are eligible.
    The stored text remains explicitly unverified and is declaration-signature
    scoped, so a statement edit makes it invisible instead of stale guidance.
    """
    if not plan_state_enabled() or function_name != "lean_reasoning_help":
        return None
    target = str(target_symbol or "").strip()
    active = str(active_file or "").strip()
    if not target or not active:
        return None
    try:
        payload = json.loads(str(result_text or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    status = str(payload.get("status", "") or "").strip()
    payload_target = str(payload.get("theorem_id", "") or "").strip()
    payload_file = str(payload.get("file_path", "") or "").strip()
    if (
        payload.get("success") is not True
        or not status.startswith("answered")
        or payload.get("truncated") is True
        or payload_target != target
        or not _same_file(payload_file, active)
    ):
        return None
    fact_text = _negative_route_fact(str(payload.get("advice", "") or ""))
    signature_sha256 = declaration_signature_sha256(target, active)
    if not fact_text or not signature_sha256:
        return None
    payload_sha256 = hashlib.sha256(str(result_text).encode("utf-8")).hexdigest()
    identity = json.dumps(
        [target, os.path.realpath(os.path.expanduser(active)), signature_sha256, fact_text],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    record = {
        "fact_id": "arf-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "campaign_id": str(campaign_id or "").strip(),
        "target_symbol": target,
        "active_file": active,
        "declaration_signature_sha256": signature_sha256,
        "fact_text": fact_text,
        "verification": VERIFICATION_LABEL,
        "source_tool": "lean_reasoning_help",
        "source_payload_sha256": payload_sha256,
        "recorded_at": _now_iso(),
    }
    outcome: dict[str, Any] | None = None

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal outcome
        durable_campaign = _campaign_id(summary)
        incoming_campaign = str(campaign_id or "").strip()
        if durable_campaign and incoming_campaign and durable_campaign != incoming_campaign:
            return
        raw_records = summary.get(SUMMARY_KEY)
        records = [dict(item) for item in raw_records or [] if isinstance(item, Mapping)]
        for existing in records:
            if str(existing.get("fact_id", "") or "") == record["fact_id"]:
                outcome = existing
                return
        records.append(dict(record))
        matching_indexes = [
            index
            for index, item in enumerate(records)
            if _record_matches_target(item, target_symbol=target, active_file=active)
        ]
        excess = max(0, len(matching_indexes) - PER_TARGET_FACT_CAP)
        drop = set(matching_indexes[:excess])
        records = [item for index, item in enumerate(records) if index not in drop]
        summary[SUMMARY_KEY] = records[-GLOBAL_FACT_CAP:]
        summary["version"] = 1
        summary["updated_at"] = _now_iso()
        outcome = dict(record)

    update_json_file(plan_state_paths().summary_json, mutate)
    return outcome


def matching_route_facts(
    summary: Mapping[str, Any] | None,
    *,
    target_symbol: str,
    active_file: str,
) -> tuple[dict[str, Any], ...]:
    """Return current-signature advisor route facts for one exact assignment."""
    state = dict(summary or {})
    target = str(target_symbol or "").strip()
    active = str(active_file or "").strip()
    signature_sha256 = declaration_signature_sha256(target, active)
    if not target or not active or not signature_sha256:
        return ()
    durable_campaign = _campaign_id(state)
    selected: list[dict[str, Any]] = []
    for raw in state.get(SUMMARY_KEY) or []:
        if not isinstance(raw, Mapping):
            continue
        record = dict(raw)
        record_campaign = str(record.get("campaign_id", "") or "").strip()
        if durable_campaign and record_campaign and durable_campaign != record_campaign:
            continue
        if not _record_matches_target(record, target_symbol=target, active_file=active):
            continue
        if str(record.get("declaration_signature_sha256", "") or "") != signature_sha256:
            continue
        if record.get("verification") != VERIFICATION_LABEL:
            continue
        selected.append(record)
    return tuple(selected[-PER_TARGET_FACT_CAP:])
