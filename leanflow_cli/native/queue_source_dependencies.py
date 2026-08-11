"""Infer exact pending declaration references from the active Lean source."""

from __future__ import annotations

import re
from pathlib import Path

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _strip_lean_comments_and_strings,
)


def pending_source_dependencies(
    active_file: str,
    queue_labels: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Return earlier pending declarations referenced by each queued declaration.

    This is a narrow source-order fact, not semantic dependency inference. A
    token-level reference in a declaration's exact source means the consumer
    cannot be completed while that earlier placeholder remains unresolved.
    """
    labels = tuple(dict.fromkeys(str(label or "").strip() for label in queue_labels))
    labels = tuple(label for label in labels if label)
    if len(labels) < 2:
        return {}
    try:
        source = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return {}
    entries = _declaration_line_index_from_text(source)
    by_name = {
        str(entry.get("name", "") or "").strip(): entry
        for entry in entries
        if str(entry.get("name", "") or "").strip()
    }
    result: dict[str, tuple[str, ...]] = {}
    for consumer in labels:
        consumer_entry = by_name.get(consumer) or by_name.get(consumer.split(".")[-1])
        if consumer_entry is None:
            continue
        consumer_line = int(consumer_entry.get("line", 0) or 0)
        consumer_text = _strip_lean_comments_and_strings(str(consumer_entry.get("text", "") or ""))
        dependencies: list[tuple[int, str]] = []
        for dependency in labels:
            if dependency == consumer:
                continue
            dependency_entry = by_name.get(dependency) or by_name.get(dependency.split(".")[-1])
            if dependency_entry is None:
                continue
            dependency_line = int(dependency_entry.get("line", 0) or 0)
            if dependency_line <= 0 or dependency_line >= consumer_line:
                continue
            short_name = dependency.split(".")[-1]
            if re.search(
                rf"(?<![\w.])(?:{re.escape(dependency)}|{re.escape(short_name)})(?![\w.])",
                consumer_text,
            ):
                dependencies.append((dependency_line, dependency))
        if dependencies:
            result[consumer] = tuple(name for _line, name in sorted(dependencies))
    return result
