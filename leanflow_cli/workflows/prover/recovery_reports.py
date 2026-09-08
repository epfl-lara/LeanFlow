"""Retain bounded verification evidence for inconclusive negation recovery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_DIAGNOSTICS = 4000


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
