"""Detect graph dependencies that Lean source order makes unavailable."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text
from leanflow_cli.workflows.plan_state import Blueprint


def _line_number(value: object) -> int:
    """Return one non-negative declaration line number."""
    try:
        return max(0, int(str(value or "0")))
    except ValueError:
        return 0


def _same_file(left: str, right: str) -> bool:
    """Return whether two source paths identify the same file."""
    if not left or not right:
        return False
    try:
        return os.path.normcase(str(Path(left).resolve(strict=False))) == os.path.normcase(
            str(Path(right).resolve(strict=False))
        )
    except OSError:
        return left == right


def later_dependency_names(
    blueprint: Blueprint,
    *,
    target_symbol: str,
    active_file: str,
    source: str,
) -> tuple[str, ...]:
    """Return proved graph dependencies declared after the assigned theorem."""
    entries = _declaration_line_index_from_text(source)
    by_name: dict[str, Mapping[str, object]] = {
        str(entry.get("name", "") or ""): entry for entry in entries
    }
    target_entry = by_name.get(target_symbol) or by_name.get(target_symbol.split(".")[-1])
    if target_entry is None:
        return ()
    target_line = _line_number(target_entry.get("line", 0))
    target_nodes = [
        node
        for node in blueprint.nodes
        if node.name == target_symbol and _same_file(node.file, active_file)
    ]
    if len(target_nodes) != 1:
        return ()
    related_ids = {target_nodes[0].id}
    changed = True
    while changed:
        changed = False
        for edge in blueprint.edges:
            dependency_id = ""
            if edge.kind == "depends_on" and edge.source in related_ids:
                dependency_id = edge.target
            elif edge.kind == "split_of" and edge.target in related_ids:
                dependency_id = edge.source
            if dependency_id and dependency_id not in related_ids:
                related_ids.add(dependency_id)
                changed = True
    later: list[tuple[int, str]] = []
    for node in blueprint.nodes:
        if (
            node.id not in related_ids
            or node.status != "proved"
            or not _same_file(node.file, active_file)
        ):
            continue
        entry = by_name.get(node.name) or by_name.get(node.name.split(".")[-1])
        line = _line_number(entry.get("line", 0)) if entry is not None else 0
        if line > target_line:
            later.append((line, node.name))
    return tuple(name for _line, name in sorted(later))


def source_order_dependency_advice(
    blueprint: Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> str:
    """Return an actionable source-order warning for the current assignment."""
    try:
        source = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return ""
    dependencies = later_dependency_names(
        blueprint,
        target_symbol=target_symbol,
        active_file=active_file,
        source=source,
    )
    if not dependencies:
        return ""
    rendered = ", ".join(f"`{name}`" for name in dependencies[:8])
    return "\n".join(
        (
            "Source-order blocker detected:",
            f"- proved graph dependencies declared after `{target_symbol}`: {rendered}",
            "- Lean cannot reference later declarations; before proving the target, move its entire "
            "preamble/declaration below the latest dependency or move a dependency-closed helper "
            "set above it",
            "- relocate existing declarations; do not synthesize duplicate bridge lemmas",
        )
    )
