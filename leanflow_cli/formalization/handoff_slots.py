"""Locate literal source proof slots using independently compiled declaration ranges."""

from __future__ import annotations

import re
from typing import Any

from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text
from leanflow_cli.workflows.prover.source import lean_code_mask, sorry_spans


def source_entries(source: str) -> list[dict[str, Any]]:
    """Locate source surgery slots, retaining namespaces for exact kernel-name matching."""
    mask = lean_code_mask(source)
    namespaces: dict[int, str] = {}
    scopes: list[str] = []
    offsets = [0]
    for number, line in enumerate(mask.splitlines(keepends=True), 1):
        namespaces[number] = ".".join(name for name in scopes if name)
        command = line.strip()
        if match := re.fullmatch(r"namespace\s+([\w.]+)", command):
            scopes.append(match.group(1))
        elif re.fullmatch(r"(?:noncomputable\s+)?section(?:\s+\w+)?|mutual", command):
            scopes.append("")
        elif re.fullmatch(r"end(?:\s+[\w.]+)?", command) and scopes:
            scopes.pop()
        offsets.append(offsets[-1] + len(line))
    spans = sorry_spans(source)
    result = []
    for entry in _declaration_line_index_from_text(mask):
        start = offsets[entry["line"] - 1]
        end = offsets[min(entry["end_line"], len(offsets) - 1)]
        holes = [index for index, (position, _) in enumerate(spans) if start <= position < end]
        namespace = namespaces.get(entry["line"], "")
        name = entry["name"]
        qualified = (
            name.removeprefix("_root_.")
            if name.startswith("_root_.")
            else f"{namespace}.{name}" if namespace else name
        )
        result.append(
            {
                **entry,
                "declaration": qualified,
                "source_name": name,
                "holes": holes,
                "hole_spans": [list(spans[index]) for index in holes],
            }
        )
    return result


def compiled_entry(
    source: str, row: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Bind literal proof holes to the compiler's exact declaration source range."""
    ranges = row.get("source_range")
    if ranges is None:
        matched = [entry for entry in entries if entry["declaration"] == row["name"]]
    else:
        selection = row.get("line_start")
        matched = [
            entry
            for entry in entries
            if ranges["line_start"] <= entry["line"] <= selection <= entry["end_line"]
        ]
        offsets = [0]
        for line in source.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        start = offsets[ranges["line_start"] - 1] + ranges["column_start"]
        end = offsets[ranges["line_end"] - 1] + ranges["column_end"]
        spans = sorry_spans(source)
        holes = [index for index, (begin, stop) in enumerate(spans) if start <= begin < stop <= end]
        matched = [
            {
                **entry,
                "holes": holes,
                "hole_spans": [list(spans[index]) for index in holes],
                "declaration_span": [start, end],
            }
            for entry in matched
        ]
    if len(matched) != 1 or not matched[0]["holes"]:
        raise ValueError(
            f"Cannot bind exact compiled obligation to literal source slots: {row['name']}"
        )
    return matched[0]
