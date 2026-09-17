"""Retain bounded tool and verification evidence for orchestrator recovery."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_DIAGNOSTICS = 4000


def recent_tool_evidence(workspace: Path) -> list[dict[str, Any]]:
    """Read bounded tool diagnostics, excluding model reasoning and large tool bodies.

    Failed attempts can predate the current runtime, so use their durable job
    events rather than requiring new result fields. A partial or damaged log
    must never prevent recovery of the obligation.
    """
    path = workspace / "events.jsonl"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        with path.open("rb") as stream:
            offset = max(0, path.stat().st_size - 1024 * 1024)
            stream.seek(offset)
            if offset:
                stream.readline()  # Discard the partial leading event.
            lines = stream.read(1024 * 1024).splitlines()
    except OSError:
        return []
    evidence: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "tool-result":
            continue
        detail = event.get("details")
        if not isinstance(detail, dict) or not isinstance(detail.get("result"), dict):
            continue
        result = detail["result"]
        item: dict[str, Any] = {
            "tool": str(detail.get("tool", ""))[:100],
            "evidence_id": str(event.get("evidence_id", ""))[:100],
        }
        for key in ("success", "ok", "timed_out"):
            if isinstance(result.get(key), bool):
                item[key] = result[key]
        for key in ("status", "error_code", "error", "hint"):
            if isinstance(result.get(key), str) and result[key]:
                item[key] = result[key][:750]
        if isinstance(result.get("results"), list):
            item["result_count"] = len(result["results"])
        arguments = detail.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        if isinstance(arguments, dict):
            item["arguments"] = {
                key: value[:350]
                for key in ("file", "declaration", "path", "query")
                if isinstance(value := arguments.get(key), str)
            }
        evidence.append(item)
        if len(evidence) == 6:
            break
    return list(reversed(evidence))


def _diagnostics(report: Mapping[str, Any], *, depth: int = 0) -> list[str]:
    """Collect checker messages from the supported nested verification reports."""
    parts: list[str] = []
    for key in ("error_code", "error", "message", "stderr", "stdout", "output"):
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value[:MAX_DIAGNOSTICS])
    messages = report.get("messages")
    if isinstance(messages, list):
        for message in messages[:20]:
            value = message.get("message", "") if isinstance(message, Mapping) else message
            if isinstance(value, str) and value.strip():
                parts.append(value[:MAX_DIAGNOSTICS])
    if depth < 3:
        for key in ("compile", "inspect", "kernel_profile"):
            nested = report.get(key)
            if isinstance(nested, Mapping):
                parts.extend(_diagnostics(nested, depth=depth + 1))
    return parts


def negation_report(outcome: Mapping[str, Any], screen: Any) -> dict[str, Any]:
    """Keep checker errors even when a file submission supplied no model notes."""
    report: dict[str, Any] = {
        "screen": screen,
        "notes": str(outcome.get("notes") or "")[:2000],
        "certified": outcome.get("certified") is True,
    }
    verification = outcome.get("verification")
    if isinstance(verification, Mapping):
        report["verification"] = {
            "accepted": verification.get("accepted") is True,
            "diagnostics": "\n".join(dict.fromkeys(_diagnostics(verification)))[:MAX_DIAGNOSTICS],
        }
    if outcome.get("evidence_path"):
        report["evidence_path"] = str(outcome["evidence_path"])
    return report


def inconclusive_finding(report: Mapping[str, Any]) -> str:
    """Describe an unsuccessful refutation without claiming the obligation is true."""
    detail = (
        "No exact negation was certified; the truth of this obligation remains unresolved. "
        "Repair concrete checker errors before repeating a refutation attempt. "
        "An unproved generated helper can be replaced through replanning."
    )
    verification = report.get("verification")
    if isinstance(verification, Mapping) and verification.get("diagnostics"):
        detail += " Checker feedback: " + str(verification["diagnostics"])
    if report.get("notes"):
        detail += " Model notes: " + str(report["notes"])
    return detail
