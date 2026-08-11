"""Score and prioritize files for project-wide ``/prove`` workflows."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.native.native_config import _read_native_env
from leanflow_cli.native.native_utils import (
    _relative_project_file_label,
    _single_line,
)

PROJECT_PROVE_MANAGER_CANDIDATE_LIMIT = 40
PROJECT_PROVE_MANAGER_FULL_FILE_MAX_CHARS = 6000
PROJECT_PROVE_MANAGER_SELECTED_FILE_MAX_CHARS = 5000
PROJECT_PROVE_MANAGER_DECL_CONTEXT_MAX_CHARS = 1600
PROJECT_PROVE_MANAGER_HINT_CONTEXT_MAX_CHARS = 1600
PROJECT_PROVE_MANAGER_PENDING_DECL_LIMIT = 8

__all__ = [
    "PROJECT_PROVE_MANAGER_CANDIDATE_LIMIT",
    "PROJECT_PROVE_MANAGER_FULL_FILE_MAX_CHARS",
    "PROJECT_PROVE_MANAGER_SELECTED_FILE_MAX_CHARS",
    "PROJECT_PROVE_MANAGER_DECL_CONTEXT_MAX_CHARS",
    "PROJECT_PROVE_MANAGER_HINT_CONTEXT_MAX_CHARS",
    "PROJECT_PROVE_MANAGER_PENDING_DECL_LIMIT",
    "_project_prove_manager_active",
    "_lean_import_modules",
    "_module_name_for_project_path",
    "_project_prove_bounded_excerpt",
    "_project_prove_declaration_context",
    "_project_prove_header_excerpt",
    "_project_prove_hint_excerpt",
    "_project_prove_file_context_excerpt",
    "_project_prove_declaration_difficulty",
    "_project_prove_file_hint_count",
    "_project_prove_worked_example_count",
    "_project_prove_file_difficulty",
    "_project_prove_dependency_graph",
    "_project_prove_transitive_paths",
    "_project_prove_label_list",
    "_project_prove_fallback_order",
    "_project_prove_priority_bucket",
    "_guard_project_prove_llm_order",
    "_ordered_labels_from_llm_payload",
    "_project_prove_manager_summary",
]


def _project_prove_manager_active(autonomy_state: Mapping[str, Any] | None = None) -> bool:
    return isinstance(autonomy_state, Mapping) and bool(
        autonomy_state.get("project_prove_manager_enabled")
    )


def _lean_import_modules(file_path: str | os.PathLike[str]) -> list[str]:
    try:
        lines = Path(file_path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    modules: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("import "):
            continue
        body = stripped[len("import ") :].split("--", 1)[0].strip()
        for token in body.split():
            module = token.strip()
            if module and module not in modules:
                modules.append(module)
    return modules


def _module_name_for_project_path(path: Path, project_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(project_root.resolve())
    except Exception:
        relative = path
    parts = list(relative.parts)
    if not parts or not parts[-1].endswith(".lean"):
        return ""
    parts[-1] = parts[-1][:-5]
    return ".".join(parts)


def _project_prove_bounded_excerpt(text: str, max_chars: int) -> str:
    raw = str(text or "").strip()
    if max_chars <= 0 or len(raw) <= max_chars:
        return raw
    marker = "\n\n-- [leanflow: excerpt truncated; middle omitted]\n\n"
    head_chars = max(0, (max_chars - len(marker)) * 2 // 3)
    tail_chars = max(0, max_chars - len(marker) - head_chars)
    return (raw[:head_chars].rstrip() + marker + raw[-tail_chars:].lstrip()).strip()


def _project_prove_declaration_context(
    lines: Sequence[str],
    entry: Mapping[str, Any],
    *,
    max_chars: int = PROJECT_PROVE_MANAGER_DECL_CONTEXT_MAX_CHARS,
) -> str:
    line = int(entry.get("line", 0) or 0)
    end_line = int(entry.get("end_line", line) or line)
    if line <= 0 or end_line <= 0:
        return ""
    start = max(1, line)
    doc_end_idx = line - 2
    if 0 <= doc_end_idx < len(lines) and lines[doc_end_idx].strip().endswith("-/"):
        idx = doc_end_idx
        while idx >= 0 and doc_end_idx - idx < 40:
            if lines[idx].strip().startswith("/-"):
                start = idx + 1
                break
            idx -= 1
    else:
        idx = line - 2
        while idx >= 0 and line - idx <= 16:
            stripped = lines[idx].strip()
            if not stripped:
                start = idx + 1
                idx -= 1
                continue
            if stripped.startswith("--"):
                start = idx + 1
                idx -= 1
                continue
            break
    start = max(1, start)
    end = min(len(lines), max(end_line, line))
    return _project_prove_bounded_excerpt("\n".join(lines[start - 1 : end]), max_chars)


def _project_prove_header_excerpt(text: str, entries: Sequence[Mapping[str, Any]]) -> str:
    lines = str(text or "").splitlines()
    first_line = min((int(entry.get("line", 0) or 0) for entry in entries), default=0)
    if first_line <= 1:
        first_line = min(len(lines) + 1, 35)
    return _project_prove_bounded_excerpt("\n".join(lines[: max(0, first_line - 1)]), 1200)


def _project_prove_hint_excerpt(text: str) -> str:
    lines = str(text or "").splitlines()
    selected: list[str] = []
    patterns = ("#check", "hint", "todo", "useful lemma", "useful mathlib")
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if not any(pattern in lowered for pattern in patterns):
            continue
        start = max(0, idx - 1)
        end = min(len(lines), idx + 2)
        block = "\n".join(lines[start:end]).strip()
        if block and block not in selected:
            selected.append(block)
    return _project_prove_bounded_excerpt(
        "\n\n".join(selected), PROJECT_PROVE_MANAGER_HINT_CONTEXT_MAX_CHARS
    )


def _project_prove_file_context_excerpt(
    text: str,
    entries: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    raw = str(text or "").strip()
    if len(raw) <= PROJECT_PROVE_MANAGER_FULL_FILE_MAX_CHARS:
        return "full", raw
    parts: list[str] = []
    header = _project_prove_header_excerpt(raw, entries)
    if header:
        parts.append("File header/imports:\n" + header)
    hints = _project_prove_hint_excerpt(raw)
    if hints:
        parts.append("Hints and checked lemmas:\n" + hints)
    pending = [dict(entry) for entry in entries if bool(entry.get("has_sorry"))]
    lines = raw.splitlines()
    for entry in pending[:2]:
        context = _project_prove_declaration_context(lines, entry, max_chars=1200)
        if context:
            parts.append(
                f"Pending declaration near line {entry.get('line', '[unknown]')}:\n{context}"
            )
    return "selected", _project_prove_bounded_excerpt(
        "\n\n".join(parts), PROJECT_PROVE_MANAGER_SELECTED_FILE_MAX_CHARS
    )


def _project_prove_declaration_difficulty(entry: Mapping[str, Any]) -> int:
    name = str(entry.get("name", "") or "").lower()
    text = str(entry.get("text", "") or "").lower()
    combined = f"{name}\n{text}"
    score = 4
    hard_tokens = {
        "putnam": 5,
        "imo": 4,
        "aime": 3,
        "amc": 2,
        "numbertheory": 2,
        "olympiad": 3,
        "convexhull": 2,
        "euclideanspace": 2,
        "volume": 2,
        "polynomial": 1,
        "finset": 1,
        "strictmono": 1,
    }
    easy_tokens = {
        "mathd": 2,
        "linear": 1,
        "lipschitz": 1,
        "abs": 1,
    }
    for token, weight in hard_tokens.items():
        if token in combined:
            score += weight
    for token, weight in easy_tokens.items():
        if token in combined:
            score -= weight
    line = int(entry.get("line", 0) or 0)
    end_line = int(entry.get("end_line", line) or line)
    span = max(1, end_line - line + 1)
    if span > 12:
        score += 1
    if span > 24:
        score += 1
    return max(0, score)


def _project_prove_file_hint_count(text: str) -> int:
    lowered = str(text or "").lower()
    return (
        len(re.findall(r"^\s*#check\b", str(text or ""), flags=re.MULTILINE))
        + lowered.count("hint")
        + lowered.count("useful lemma")
        + lowered.count("useful mathlib")
    )


def _project_prove_worked_example_count(
    entries: Sequence[Mapping[str, Any]], first_pending_line: int
) -> int:
    return sum(
        1
        for entry in entries
        if int(entry.get("line", 0) or 0) < first_pending_line
        and not bool(entry.get("has_sorry"))
        and str(entry.get("kind", "") or "") in {"example", "theorem", "lemma"}
    )


def _project_prove_file_difficulty(
    entries: Sequence[Mapping[str, Any]],
    text: str,
) -> dict[str, Any]:
    pending = [dict(entry) for entry in entries if bool(entry.get("has_sorry"))]
    file_context_kind, file_context_excerpt = _project_prove_file_context_excerpt(text, entries)
    if not pending:
        return {
            "difficulty_score": 0,
            "first_pending_difficulty_score": 0,
            "hint_count": _project_prove_file_hint_count(text),
            "worked_example_count": 0,
            "pending_declarations": [],
            "file_context_kind": file_context_kind,
            "file_context_excerpt": file_context_excerpt,
        }
    lines = str(text or "").splitlines()
    pending_declarations: list[dict[str, Any]] = [
        {
            "name": str(entry.get("name", "") or ""),
            "kind": str(entry.get("kind", "") or ""),
            "line": int(entry.get("line", 0) or 0),
            "difficulty_score": _project_prove_declaration_difficulty(entry),
            "context_excerpt": _project_prove_declaration_context(lines, entry),
        }
        for entry in pending[:PROJECT_PROVE_MANAGER_PENDING_DECL_LIMIT]
    ]
    first_line = int(pending[0].get("line", 0) or 0)
    hint_count = _project_prove_file_hint_count(text)
    worked_example_count = _project_prove_worked_example_count(entries, first_line)
    first_score = int(pending_declarations[0].get("difficulty_score", 0) or 0)
    average_score = round(
        sum(int(item.get("difficulty_score", 0) or 0) for item in pending_declarations)
        / len(pending_declarations),
        2,
    )
    support_discount = min(4, hint_count // 2 + worked_example_count // 3)
    difficulty_score = max(0, int(round((first_score * 2 + average_score) / 3)) - support_discount)
    return {
        "difficulty_score": difficulty_score,
        "first_pending_difficulty_score": first_score,
        "hint_count": hint_count,
        "worked_example_count": worked_example_count,
        "pending_declarations": pending_declarations,
        "file_context_kind": file_context_kind,
        "file_context_excerpt": file_context_excerpt,
    }


def _project_prove_dependency_graph(
    lean_files: Sequence[Path],
    module_to_path: Mapping[str, Path],
) -> tuple[dict[Path, set[Path]], dict[Path, set[Path]], dict[Path, list[str]]]:
    imports_by_path: dict[Path, set[Path]] = {path.resolve(): set() for path in lean_files}
    imported_by_path: dict[Path, set[Path]] = {path.resolve(): set() for path in lean_files}
    import_modules_by_path: dict[Path, list[str]] = {}
    for path in lean_files:
        resolved = path.resolve()
        modules = _lean_import_modules(path)
        import_modules_by_path[resolved] = modules
        for module in modules:
            imported = module_to_path.get(module)
            if imported is None:
                continue
            imported_resolved = imported.resolve()
            if imported_resolved == resolved:
                continue
            imports_by_path.setdefault(resolved, set()).add(imported_resolved)
            imported_by_path.setdefault(imported_resolved, set()).add(resolved)
    return imports_by_path, imported_by_path, import_modules_by_path


def _project_prove_transitive_paths(start: Path, graph: Mapping[Path, set[Path]]) -> set[Path]:
    root = start.resolve()
    seen: set[Path] = set()
    stack = list(graph.get(root, set()))
    while stack:
        current = stack.pop()
        if current in seen or current == root:
            continue
        seen.add(current)
        stack.extend(path for path in graph.get(current, set()) if path not in seen)
    return seen


def _project_prove_label_list(paths: Iterable[Path], root: Path, *, limit: int = 8) -> list[str]:
    labels = [
        _relative_project_file_label(path, root)
        for path in sorted(paths, key=lambda value: str(value))
    ]
    return [label for label in labels if label][:limit]


def _project_prove_fallback_order(candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    ordered = sorted(
        (dict(item) for item in candidates),
        key=lambda item: (
            -int(item.get("candidate_downstream_count", 0) or 0),
            int(item.get("candidate_import_count", 0) or 0),
            -int(item.get("project_downstream_count", 0) or 0),
            -int(item.get("imported_by_count", 0) or 0),
            int(item.get("difficulty_score", 0) or 0),
            int(item.get("first_pending_difficulty_score", 0) or 0),
            int(item.get("sorry_count", 0) or 0),
            int(item.get("line_count", 0) or 0),
            int(item.get("declaration_count", 0) or 0),
            str(item.get("label", "") or ""),
        ),
    )
    return [
        str(item.get("label", "") or "") for item in ordered if str(item.get("label", "") or "")
    ]


def _project_prove_priority_bucket(candidate: Mapping[str, Any]) -> tuple[int, ...]:
    return (
        -int(candidate.get("candidate_downstream_count", 0) or 0),
        int(candidate.get("candidate_import_count", 0) or 0),
        -int(candidate.get("project_downstream_count", 0) or 0),
        -int(candidate.get("imported_by_count", 0) or 0),
        int(candidate.get("difficulty_score", 0) or 0) // 3,
        int(candidate.get("first_pending_difficulty_score", 0) or 0) // 3,
    )


def _guard_project_prove_llm_order(
    labels: Sequence[str],
    candidates: Sequence[Mapping[str, Any]],
) -> list[str]:
    fallback = _project_prove_fallback_order(candidates)
    if not labels:
        return fallback
    candidate_by_label = {str(item.get("label", "") or ""): dict(item) for item in candidates}
    llm_rank = {str(label): idx for idx, label in enumerate(labels)}
    fallback_rank = {str(label): idx for idx, label in enumerate(fallback)}
    return sorted(
        fallback,
        key=lambda label: (
            _project_prove_priority_bucket(candidate_by_label.get(label, {})),
            llm_rank.get(label, len(llm_rank) + fallback_rank.get(label, 0)),
            fallback_rank.get(label, 0),
        ),
    )


def _ordered_labels_from_llm_payload(payload: Any, valid_labels: set[str]) -> list[str]:
    if isinstance(payload, Mapping):
        raw_items = (
            payload.get("files")
            or payload.get("queue")
            or payload.get("ordered_files")
            or payload.get("order")
            or []
        )
    else:
        raw_items = payload if isinstance(payload, list) else []
    labels: list[str] = []
    for item in raw_items:
        if isinstance(item, Mapping):
            label = str(item.get("label") or item.get("file") or item.get("path") or "").strip()
        else:
            label = str(item or "").strip()
        if label in valid_labels and label not in labels:
            labels.append(label)
    return labels


def _project_prove_manager_summary(autonomy_state: Mapping[str, Any] | None) -> str:
    if not _project_prove_manager_active(autonomy_state):
        return ""
    state = dict(autonomy_state or {})
    queue = [
        str(item or "") for item in state.get("project_prove_file_queue", []) if str(item or "")
    ]
    active = str(
        state.get("project_prove_active_file", "") or _read_native_env("ACTIVE_FILE", "") or ""
    ).strip()
    completed = [
        str(item or "")
        for item in state.get("project_prove_completed_files", [])
        if str(item or "")
    ]
    lines = [
        "Project prove manager:",
        f"- active file: {active or '[none assigned]'}",
        f"- queue source: {state.get('project_prove_plan_source', '[unknown]')}",
    ]
    reason = str(state.get("project_prove_plan_reason", "") or "").strip()
    if reason:
        lines.append(f"- prioritization reason: {_single_line(reason, 220)}")
    if queue:
        shown = queue[:8]
        lines.append(
            "- file queue: "
            + ", ".join(shown)
            + (f", ... plus {len(queue) - len(shown)} more" if len(queue) > len(shown) else "")
        )
    else:
        lines.append("- file queue: [empty]")
    if completed:
        lines.append("- completed files: " + ", ".join(completed[-6:]))
    return "\n".join(lines)
