"""Compact job history deterministically while retaining its complete contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def approximate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate input tokens conservatively without calling a model."""
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) // 3 + 1


def compact_history(
    messages: list[dict[str, Any]], *, context_tokens: int, workspace: Path
) -> tuple[list[dict[str, Any]], bool]:
    """Retain pinned instructions, durable notes, and complete recent exchanges.

    Never retain an orphan tool result. The original prompt is immutable and
    includes the assignment, plan, DAG and budget contract on every request.
    """
    if approximate_tokens(messages) <= context_tokens:
        return messages, False
    notes_path = workspace / "PLAN_job.md"
    notes = notes_path.read_text(encoding="utf-8")[:16000] if notes_path.is_file() else ""
    pinned = messages[:2]
    if notes:
        pinned = pinned + [{"role": "user", "content": "Current proof notes:\n" + notes}]
    groups: list[list[dict[str, Any]]] = []
    for message in messages[2:]:
        if message.get("role") != "tool" or not groups:
            groups.append([])
        groups[-1].append(message)
    retained: list[dict[str, Any]] = []
    for group in reversed(groups):
        candidate = pinned + group + retained
        if approximate_tokens(candidate) > context_tokens:
            break
        retained = group + retained
    return pinned + retained, True
