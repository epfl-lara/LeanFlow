"""Project managed search results onto the assigned declaration's source horizon."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from leanflow_cli.lean.lean_declarations import _declaration_index

__all__ = ["enrich_local_source_results", "partition_source_order_results"]


def _canonical_path(value: object, *, cwd: str) -> Path | None:
    """Resolve one path without accepting an empty value."""
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(cwd or ".").expanduser() / path
    return path.resolve(strict=False)


def _short_name(value: object) -> str:
    """Return the final Lean name component used by the source index."""
    return str(value or "").strip().split(".")[-1]


def _unique_declaration(
    entries: list[dict[str, Any]],
    symbol: object,
) -> dict[str, Any] | None:
    """Return the unique local declaration matching a full or short symbol."""
    wanted = str(symbol or "").strip()
    short = _short_name(wanted)
    if not wanted or not short:
        return None
    matches = [
        entry for entry in entries if str(entry.get("name", "") or "").strip() in {wanted, short}
    ]
    return dict(matches[0]) if len(matches) == 1 else None


def _unique_declaration_at_line(
    entries: list[dict[str, Any]],
    line: object,
) -> dict[str, Any] | None:
    """Return the unique declaration whose source begins at an exact line."""
    try:
        wanted = int(str(line or "0").strip())
    except (TypeError, ValueError):
        return None
    if wanted <= 0:
        return None
    matches = [entry for entry in entries if int(entry.get("line", 0) or 0) == wanted]
    return dict(matches[0]) if len(matches) == 1 else None


def _lean_module_component(component: str) -> str:
    """Render one file component in the conventional Lean module spelling."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*", component):
        return component
    return f"«{component}»"


def _active_module_name(active_file: Path, *, cwd: str) -> str:
    """Return the project-relative Lean module name when it is unambiguous."""
    root = _canonical_path(cwd, cwd="")
    if root is None:
        return ""
    try:
        relative = active_file.relative_to(root)
    except ValueError:
        return ""
    without_suffix = relative.with_suffix("")
    return ".".join(_lean_module_component(part) for part in without_suffix.parts)


def _source_link_matches_active_file(
    source_link: object,
    *,
    active_file: Path,
    cwd: str,
) -> bool:
    """Return whether a provider link names the exact active project-relative file."""
    text = str(source_link or "").strip()
    root = _canonical_path(cwd, cwd="")
    if not text or root is None:
        return False
    try:
        relative = active_file.relative_to(root).as_posix()
    except ValueError:
        return False
    path = unquote(urlparse(text).path).rstrip("/")
    return path == relative or path.endswith(f"/{relative}")


def _result_is_same_file(
    result: Mapping[str, Any],
    *,
    active_file: Path,
    cwd: str,
) -> bool:
    """Confirm same-file identity using structured path, module, or source-link data."""
    result_file = _canonical_path(result.get("file"), cwd=cwd)
    if result_file is not None and result_file == active_file:
        return True
    active_module = _active_module_name(active_file, cwd=cwd)
    result_module = str(result.get("module", "") or "").strip()
    if active_module and result_module == active_module:
        return True
    return _source_link_matches_active_file(
        result.get("source_link"),
        active_file=active_file,
        cwd=cwd,
    )


def _source_namespace_names(active_file: Path) -> set[str]:
    """Return namespace names explicitly opened by the active source file."""
    try:
        source = active_file.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {
        match.group(1)
        for match in re.finditer(
            r"(?m)^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.]*)\s*(?:--.*)?$",
            source,
        )
    }


def enrich_local_source_results(
    payload: Mapping[str, Any],
    *,
    active_file: str = "",
    cwd: str = "",
) -> dict[str, Any]:
    """Attach exact active-file declarations to unstructured local-search symbols.

    The MCP local index sometimes returns only ``match=Namespace.privateName``.
    Resolve that value only when its short name is unique in the active file and
    its qualifier agrees with an explicit local namespace (or it is unqualified).
    This both exposes the usable declaration signature and gives the source-order
    fence enough evidence to hide current or future declarations.
    """
    enriched = dict(payload)
    active = _canonical_path(active_file, cwd=cwd)
    raw_results = enriched.get("results")
    if active is None or not active.is_file() or not isinstance(raw_results, list):
        return enriched
    entries = _declaration_index(active)
    namespaces = _source_namespace_names(active)
    changed = False
    results: list[Any] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping) or str(raw.get("provider", "") or "") != (
            "mcp-local-search"
        ):
            results.append(raw)
            continue
        result = dict(raw)
        symbol = str(result.get("name", "") or result.get("match", "") or "").strip()
        if not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_']*\.)*[A-Za-z_][A-Za-z0-9_']*", symbol):
            results.append(raw)
            continue
        qualifier, _, short = symbol.rpartition(".")
        if qualifier and qualifier not in namespaces:
            results.append(raw)
            continue
        declaration = _unique_declaration(entries, short)
        if declaration is None:
            results.append(raw)
            continue
        result.update(
            {
                "name": str(declaration.get("name", "") or short),
                "file": str(active),
                "line": int(declaration.get("line", 0) or 0),
                "end_line": int(declaration.get("end_line", 0) or 0),
                "declaration": str(declaration.get("text", "") or ""),
                "local_source_enriched": True,
            }
        )
        results.append(result)
        changed = True
    if changed:
        enriched["results"] = results
    return enriched


def partition_source_order_results(
    payload: Mapping[str, Any],
    *,
    active_file: str = "",
    target_symbol: str = "",
    cwd: str = "",
) -> dict[str, Any]:
    """Move confirmed current-or-later same-file declarations out of usable results.

    Classification uses the current file index rather than provider line
    metadata. Project-rg is the narrow exception: its name-less result is
    resolved only when the match occurs exactly on a declaration's start line.
    Ambiguous names and uncertain file identity fail open so this managed
    projection cannot hide imported or otherwise usable declarations.
    """
    projected = dict(payload)
    active = _canonical_path(active_file, cwd=cwd)
    if active is None or not target_symbol or not active.is_file():
        return projected
    entries = _declaration_index(active)
    assigned = _unique_declaration(entries, target_symbol)
    if assigned is None:
        return projected
    try:
        assigned_line = int(assigned.get("line", 0) or 0)
    except (TypeError, ValueError):
        return projected
    if assigned_line <= 0:
        return projected

    raw_results = projected.get("results")
    if not isinstance(raw_results, list):
        return projected
    usable: list[Any] = []
    inaccessible: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping) or not _result_is_same_file(
            raw,
            active_file=active,
            cwd=cwd,
        ):
            usable.append(raw)
            continue
        declaration = _unique_declaration(entries, raw.get("name"))
        if (
            declaration is None
            and str(raw.get("provider", "") or "") == "project-rg"
            and not str(raw.get("name", "") or "").strip()
        ):
            declaration = _unique_declaration_at_line(entries, raw.get("line"))
        if declaration is None:
            usable.append(raw)
            continue
        try:
            current_line = int(declaration.get("line", 0) or 0)
        except (TypeError, ValueError):
            usable.append(raw)
            continue
        if current_line < assigned_line:
            usable.append(raw)
            continue
        is_assigned = current_line == assigned_line
        inaccessible.append(
            {
                **dict(raw),
                "source_access": (
                    "assigned_declaration_unavailable"
                    if is_assigned
                    else "future_same_file_unavailable"
                ),
                "usable_in_assigned_proof": False,
                "assigned_source_line": assigned_line,
                "current_source_line": current_line,
                "reason": (
                    "the assigned declaration cannot use itself recursively"
                    if is_assigned
                    else "declared after the managed assignment"
                ),
            }
        )

    if not inaccessible:
        return projected
    projected["results"] = usable
    projected["source_order_inaccessible_results"] = inaccessible
    projected["source_order_inaccessible_count"] = len(inaccessible)
    projected["source_order_guidance"] = (
        "These results name the assigned declaration itself or declarations later in the active "
        "file, so they are unavailable in the assigned proof. Do not submit their names to tactic "
        "screening; use prior or imported results."
    )
    return projected
