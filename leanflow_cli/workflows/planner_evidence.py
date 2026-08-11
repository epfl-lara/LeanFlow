"""Persist exact-target advisor evidence for later planner synthesis."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.workflows import plan_state

SUMMARY_KEY = "planner_advisor_evidence"
EVIDENCE_HISTORY_CAP = 8
EVIDENCE_TEXT_CAP = 16_000
PROMPT_EVIDENCE_CAP = 28_000


def _same_file(left: str, right: str) -> bool:
    """Return whether two file labels identify the same path."""
    if left == right:
        return True
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except OSError:
        return False


def _evidence_text(result_text: str, payload: Mapping[str, Any]) -> str:
    """Return the most useful bounded advisor response text."""
    for key in ("answer", "advice", "response", "result", "content", "output", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:EVIDENCE_TEXT_CAP]
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    return (encoded if encoded != "{}" else str(result_text or "").strip())[:EVIDENCE_TEXT_CAP]


def record_advisor_evidence(
    *,
    result_text: str,
    payload: Mapping[str, Any],
    target_symbol: str,
    active_file: str,
    target_declaration_sha256: str = "",
    source: str = "lean_reasoning_help",
) -> bool:
    """Persist one successful advisor response under its exact assignment."""
    target = str(target_symbol or "").strip()
    file_path = str(active_file or "").strip()
    text = _evidence_text(result_text, payload)
    if not target or not file_path or not text:
        return False
    summary = plan_state.load_summary()
    records = [dict(item) for item in (summary.get(SUMMARY_KEY) or []) if isinstance(item, Mapping)]
    record = {
        "target_symbol": target,
        "active_file": file_path,
        "target_declaration_sha256": str(target_declaration_sha256 or "").strip(),
        "source": str(source or "lean_reasoning_help").strip(),
        "text": text,
    }
    identity = (
        record["target_symbol"],
        record["active_file"],
        record["target_declaration_sha256"],
        record["text"],
    )
    records = [
        item
        for item in records
        if (
            str(item.get("target_symbol", "") or ""),
            str(item.get("active_file", "") or ""),
            str(item.get("target_declaration_sha256", "") or ""),
            str(item.get("text", "") or ""),
        )
        != identity
    ]
    records.append(record)
    summary[SUMMARY_KEY] = records[-EVIDENCE_HISTORY_CAP:]
    plan_state.save_summary(summary)
    plan_state.append_journal_event(
        {
            "event": "planner-advisor-evidence-persisted",
            "target_symbol": target,
            "active_file": file_path,
            "target_declaration_sha256": record["target_declaration_sha256"],
            "evidence_chars": len(text),
        }
    )
    return True


def matching_advisor_evidence(
    *,
    target_symbol: str,
    active_file: str,
    target_declaration_sha256: str = "",
) -> tuple[dict[str, str], ...]:
    """Return current-signature advisor evidence for one exact assignment."""
    target = str(target_symbol or "").strip()
    file_path = str(active_file or "").strip()
    signature = str(target_declaration_sha256 or "").strip()
    matches: list[dict[str, str]] = []
    for raw in plan_state.load_summary().get(SUMMARY_KEY) or []:
        if not isinstance(raw, Mapping):
            continue
        item_target = str(raw.get("target_symbol", "") or "").strip()
        item_file = str(raw.get("active_file", "") or "").strip()
        item_signature = str(raw.get("target_declaration_sha256", "") or "").strip()
        text = str(raw.get("text", "") or "").strip()
        if item_target != target or not _same_file(item_file, file_path) or not text:
            continue
        if signature and item_signature and signature != item_signature:
            continue
        matches.append(
            {
                "source": str(raw.get("source", "") or "lean_reasoning_help"),
                "text": text[:EVIDENCE_TEXT_CAP],
            }
        )
    return tuple(matches)


def prompt_payload(evidence: Sequence[Mapping[str, Any]]) -> str:
    """Render bounded planner evidence without changing its mathematical text."""
    payload = [
        {
            "source": str(item.get("source", "") or "advisor"),
            "text": str(item.get("text", "") or "")[:EVIDENCE_TEXT_CAP],
        }
        for item in evidence
        if str(item.get("text", "") or "").strip()
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= PROMPT_EVIDENCE_CAP:
        return encoded
    return encoded[: PROMPT_EVIDENCE_CAP - 39] + "...[remaining evidence stays durable]"
