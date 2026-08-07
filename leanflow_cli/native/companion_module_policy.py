"""Recommend dependency-safe Lean companion modules before active files sprawl."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_LINE_THRESHOLD = 600
DEFAULT_BYTE_THRESHOLD = 64 * 1024


def active_module_name(active_file: str, *, project_root: str) -> str:
    """Return the project-relative module name for an active Lean source file."""
    path = Path(str(active_file or "")).resolve(strict=False)
    root = Path(str(project_root or ".")).resolve(strict=False)
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    return ".".join(relative.with_suffix("").parts)


def imports_active_module(source: str, active_file: str, *, project_root: str) -> bool:
    """Return whether companion source reverse-imports its active module."""
    module_name = active_module_name(active_file, project_root=project_root)
    return any(
        line.strip() == f"import {module_name}"
        for line in str(source or "").splitlines()
        if line.lstrip().startswith("import ")
    )


def _positive_env(name: str, default: int) -> int:
    """Return one positive integer environment override."""
    try:
        value = int(str(os.getenv(name, default) or default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def companion_module_advice(active_file: str, *, project_root: str) -> str:
    """Return mandatory placement guidance when a Lean file is large."""
    path = Path(str(active_file or ""))
    if path.suffix != ".lean" or path.stem.endswith("Helpers"):
        return ""
    try:
        source = path.read_text(encoding="utf-8")
        size = path.stat().st_size
    except OSError:
        return ""
    line_count = source.count("\n") + (1 if source else 0)
    line_threshold = _positive_env(
        "LEANFLOW_COMPANION_MODULE_LINE_THRESHOLD",
        DEFAULT_LINE_THRESHOLD,
    )
    byte_threshold = _positive_env(
        "LEANFLOW_COMPANION_MODULE_BYTE_THRESHOLD",
        DEFAULT_BYTE_THRESHOLD,
    )
    if line_count < line_threshold and size < byte_threshold:
        return ""
    companion = path.with_name(f"{path.stem}Helpers.lean")
    try:
        root = Path(project_root).resolve(strict=False)
        relative = companion.resolve(strict=False).relative_to(root)
        active_relative = path.resolve(strict=False).relative_to(root)
        module_name = ".".join(relative.with_suffix("").parts)
        active_module = ".".join(active_relative.with_suffix("").parts)
        companion_label = str(relative)
    except (OSError, ValueError):
        module_name = companion.stem
        active_module = path.stem
        companion_label = str(companion)
    companion_exists = companion.is_file()
    import_line = f"import {module_name}"
    imported = any(
        line.strip() == import_line
        for line in source.splitlines()
        if line.lstrip().startswith("import ")
    )
    reverse_import = False
    if companion_exists:
        try:
            reverse_import = imports_active_module(
                companion.read_text(encoding="utf-8"),
                str(path),
                project_root=project_root,
            )
        except OSError:
            pass
    lines = [
        "Companion-module policy:",
        f"- active file size: {line_count} lines / {size} bytes",
        f"- preferred generic-helper module: `{companion_label}` (`{module_name}`)",
        f"- companion status: {'exists' if companion_exists else 'missing'}; "
        f"active import status: {'present' if imported else 'missing'}",
        "- mandatory placement decision before every new top-level helper: if it is "
        "self-contained over Mathlib/general imports, place it in the companion module, "
        "not in this oversized target file",
        f"- create/import with `{import_line}` when the first dependency-safe helper is added",
        "- keep target-specific or private-dependency helpers beside their target; Lean private "
        "declarations cannot be imported across modules",
        "- treat companion creation plus the active-file import as one change and verify the imported "
        "active module before continuing",
        "- do not duplicate an existing helper merely to force it across the module boundary",
        "- in the final report, state the placement decision for every newly banked helper "
        "(companion or target-local, with the dependency reason)",
    ]
    if reverse_import:
        lines.extend(
            [
                f"- unsafe reverse import detected: `{companion_label}` imports the active module "
                f"`{active_module}`",
                "- do not use observations from that reverse-import companion as current-source "
                "authority: Lean may load a stale compiled active module; remove the reverse import "
                "and keep only helpers self-contained over earlier/general modules",
            ]
        )
    return "\n".join(lines)
