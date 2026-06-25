#!/usr/bin/env python3
"""Pure Lean module-name / import-path helpers extracted from native_runner.

These functions translate between Lean source text, module names, and on-disk file
paths. They have no native_runner-only state: their only non-stdlib callees are the
already-extracted leaf helpers ``_strip_lean_comments_and_strings`` (lean_parsing),
``_blueprint_import_plan_section`` (formalization_document_runner), and
``_project_root`` (native_config), so this module imports those directly and never
imports native_runner — no import cycle. native_runner re-exports every name below so
all existing callers and tests keep resolving ``native_runner.<name>`` unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

from leanflow_cli.formalization.formalization_document_runner import _blueprint_import_plan_section
from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings
from leanflow_cli.native.native_config import _project_root


def _lean_imports_from_text(text: str) -> list[str]:
    imports: list[str] = []
    for match in re.finditer(
        r"^\s*import\s+([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\b",
        _strip_lean_comments_and_strings(str(text or "")),
        flags=re.MULTILINE,
    ):
        module = match.group(1).strip()
        if module and module not in imports:
            imports.append(module)
    return imports


def _lean_imports_from_file(path: Path) -> list[str]:
    try:
        return _lean_imports_from_text(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _valid_lean_module_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return all(re.match(r"^[A-Za-z_][A-Za-z0-9_']*$", part) for part in text.split("."))


def _blueprint_import_plan_imports(text: str) -> list[str]:
    section = _blueprint_import_plan_section(text)
    if not section:
        return []
    imports: list[str] = []

    def _add(module: str) -> None:
        normalized = str(module or "").strip()
        if normalized.endswith(".lean") or "/" in normalized:
            return
        if _valid_lean_module_name(normalized) and normalized not in imports:
            imports.append(normalized)

    for match in re.finditer(
        r"^\s*import\s+([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\b",
        section,
        flags=re.MULTILINE,
    ):
        _add(match.group(1))
    in_non_direct_subsection = False
    for line in section.splitlines():
        lowered = line.lower()
        if re.match(r"^\s*#{1,6}\s+", line) or re.match(
            r"^\s*(?:direct|suggested|search|notes?)\s*:", lowered
        ):
            in_non_direct_subsection = any(
                token in lowered
                for token in ("suggest", "search", "candidate", "transitive", "note")
            )
            continue
        if any(
            token in lowered
            for token in (
                "suggested search",
                "search module",
                "candidate module",
                "transitive",
                "not required",
                "prover may",
            )
        ):
            continue
        if in_non_direct_subsection:
            continue
        if not line.lstrip().startswith("-"):
            continue
        code_span = re.search(r"`([^`]+)`", line)
        if code_span:
            _add(code_span.group(1))
            continue
        bullet = re.search(
            r"-\s*([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\b",
            line,
        )
        if bullet:
            _add(bullet.group(1))
    return imports


def _lean_decl_names_from_planned_value(value: str) -> list[str]:
    names: list[str] = []

    def _add(raw: str) -> None:
        candidate = str(raw or "").strip()
        candidate = candidate.split(":", 1)[0].strip()
        candidate = candidate.split(" ", 1)[0].strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_'.]*$", candidate) and candidate not in names:
            names.append(candidate)

    for span in re.findall(r"`([^`]+)`", str(value or "")):
        _add(span)
    if not names:
        for chunk in re.split(r"[,;]|\band\b", str(value or "")):
            _add(chunk)
    return names


def _root_module_file_for_module(module_name: str) -> tuple[str, Path] | None:
    if not module_name or "." not in module_name:
        return None
    root_module = module_name.split(".", 1)[0]
    if not _valid_lean_module_name(root_module):
        return None
    return root_module, Path(_project_root()) / f"{root_module}.lean"


def _module_file_for_module(module_name: str) -> Path | None:
    if not _valid_lean_module_name(module_name):
        return None
    return Path(_project_root()) / Path(*module_name.split(".")).with_suffix(".lean")


def _module_name_for_file(active_file: str) -> str:
    if not active_file:
        return ""
    try:
        relative = Path(active_file).resolve().relative_to(Path(_project_root()).resolve())
    except Exception:
        return ""
    parts = list(relative.parts)
    if not parts or not parts[-1].endswith(".lean"):
        return ""
    parts[-1] = parts[-1][:-5]
    if any(not re.match(r"^[A-Za-z_][A-Za-z0-9_']*$", part) for part in parts):
        return ""
    return ".".join(parts)
