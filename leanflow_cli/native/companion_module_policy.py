"""Recommend dependency-safe Lean companion modules before active files sprawl."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_LINE_THRESHOLD = 600
DEFAULT_BYTE_THRESHOLD = 64 * 1024


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
        relative = companion.resolve(strict=False).relative_to(
            Path(project_root).resolve(strict=False)
        )
        module_name = ".".join(relative.with_suffix("").parts)
        companion_label = str(relative)
    except (OSError, ValueError):
        module_name = companion.stem
        companion_label = str(companion)
    companion_exists = companion.is_file()
    import_line = f"import {module_name}"
    imported = any(
        line.strip() == import_line
        for line in source.splitlines()
        if line.lstrip().startswith("import ")
    )
    return "\n".join(
        [
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
    )
