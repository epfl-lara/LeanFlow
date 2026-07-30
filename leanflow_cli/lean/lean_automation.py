"""Normalize native Lean automation results for ``lean_services``.

The helpers classify backend failures, extract diagnostics, validate probe
attempts, build source replacements, and choose search depth. Backend invocation
and mutable service state remain in ``lean_services``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _native_backend_status_indicates_failure(payload: Mapping[str, Any]) -> bool:
    failure_statuses = {"fail", "rejected", "failed", "failure", "error", "invalid"}
    for key in ("status", "outcome", "validation_status", "result_status"):
        status = str(payload.get(key, "") or "").strip().lower()
        if status in failure_statuses:
            return True
    return False


def _native_backend_failure_message(payload: Mapping[str, Any]) -> str:
    for key in ("error_message", "error", "message", "failure", "reason", "status"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return " ".join(value.split())[:500]
    return ""


def _proof_auto_harness_failure_message(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("error_message", "error", "message", "failure", "reason", "status", "text"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            parts.append(value)
    parts.extend(
        str(reason) for reason in list(payload.get("degraded_reasons") or []) if str(reason).strip()
    )
    text = " ".join(" ".join(part.split()) for part in parts)
    lowered = text.lower()
    if "failed to construct harness" in lowered or "unsafe value range shape" in lowered:
        return text[:700] or "proof-auto backend failed to construct a proof harness"
    return ""


UNSUPPORTED_PROOF_AUTO_OPTIONS = {
    "linter.style.longLine": "lean-auto-try backend does not support project-level `set_option linter.style.longLine`",
}


def _proof_auto_unsupported_option_reason(file_path: str | os.PathLike[str]) -> str:
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except OSError:
        return ""
    for option, reason in UNSUPPORTED_PROOF_AUTO_OPTIONS.items():
        if re.search(rf"(?m)^\s*set_option\s+{re.escape(option)}\b", text):
            return reason
    return ""


def _auto_probe_attempt_succeeded(payload: Mapping[str, Any]) -> bool:
    if bool(payload.get("success", False)):
        return True
    classification = str(payload.get("classification", "") or "").strip().lower()
    if classification in {"trivial", "promising", "solved", "success"}:
        return True
    status = str(payload.get("status", "") or "").strip().lower()
    return status in {"trivial", "promising", "solved", "success"}


def _automation_probe_replacement(entry: Mapping[str, Any], method: str) -> str:
    text = str(entry.get("text", "") or "").strip()
    tactic = str(method or "").strip()
    if not text or not tactic:
        return ""
    match = re.search(r":=\s*by\b", text)
    if match:
        return text[: match.end()].rstrip() + f"\n  {tactic}\n"
    if re.search(r"\b(sorry|by)\b", text):
        return re.sub(r"\b(sorry|by\s+.*)\s*$", f"by\n  {tactic}", text, count=1, flags=re.DOTALL)
    return ""


def _incremental_probe_diagnostics(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    diagnostics = []
    for message in list(result.get("messages") or [])[:6]:
        if not isinstance(message, Mapping):
            continue
        location = None
        file_start = message.get("file_start")
        if isinstance(file_start, Mapping):
            location = {
                "file": str(result.get("file", "") or ""),
                "line": file_start.get("line"),
                "column": file_start.get("column"),
            }
        diagnostics.append(
            {
                "severity": str(message.get("severity", "") or "info"),
                "message": str(message.get("message", "") or ""),
                "location": location,
            }
        )
    if not diagnostics and str(result.get("error", "") or "").strip():
        diagnostics.append(
            {
                "severity": "error",
                "message": str(result.get("error", "") or ""),
                "location": None,
            }
        )
    return diagnostics


def _auto_search_depth_for_objective(objective: str) -> str:
    normalized = str(objective or "").strip().lower()
    mapping = {
        "quick": "quick",
        "fast": "quick",
        "balanced": "normal",
        "normal": "normal",
        "deep": "deep",
        "thorough": "deep",
        "exhaustive": "exhaustive",
    }
    return mapping.get(normalized, "normal")
